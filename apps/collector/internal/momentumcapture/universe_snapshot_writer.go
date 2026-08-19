package momentumcapture

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/mavlevich/schurfer/collector/internal/momentumsource"
)

// pgUniqueViolation is Postgres's own SQLSTATE code for a unique/primary-key
// constraint violation (23505). Used to detect a concurrent writer that won
// the race to insert the same snapshot row -- see PersistUniverseSnapshot's
// own doc comment on why this is treated as a retry, not a hard failure.
const pgUniqueViolation = "23505"

// errSnapshotInsertRaceLost marks a unique_violation on the snapshot ROW's
// own INSERT specifically -- deliberately NOT raised for a unique_violation
// on the per-instrument batch insert that follows it in the same
// transaction (that one is a real caller bug, e.g. a catalog with a
// duplicate native_market_id, exactly what
// TestPersistUniverseSnapshotIsAtomicOnAConstraintViolation exercises, and
// must surface as a hard error, not get reinterpreted as a race). Never
// returned to a caller directly; PersistUniverseSnapshot checks for it via
// errors.Is to decide whether to recheck against the row that won.
var errSnapshotInsertRaceLost = errors.New("momentumcapture: lost a concurrent race inserting the universe snapshot row")

// IdentitySchemaVersion is the normalization/parsing schema version stamped
// on every persisted universe snapshot. catalog_version's own hash already
// changes whenever an instrument's CONTENT changes; schema_version exists
// for the separate case where the same raw venue data would now be
// classified differently because this package's own parsing/validation
// logic changed, not the venue's -- bump this whenever classifyCatalogItem
// (bybit or binance) or momentumsource.NewInstrument's own rules change in
// a way that could reclassify an otherwise-unchanged instrument
// differently.
const IdentitySchemaVersion = "v1"

// ErrSnapshotPayloadMismatch is returned when a snapshot already exists
// under the same (exchange, universe_version, catalog_version) key but
// with a different payload_hash -- see PersistUniverseSnapshot's own doc
// comment. Never silently overwritten.
var ErrSnapshotPayloadMismatch = errors.New(
	"momentumcapture: universe snapshot payload mismatch for an existing (exchange, universe_version, catalog_version) key",
)

// universeSnapshotDB is the one method PersistUniverseSnapshot actually
// needs: a real transaction, for the atomic all-or-nothing write this type
// exists for. Deliberately not writerDB (SendBatch/Close only, no Begin):
// Writer's own high-volume, best-effort, sub-batched bar writes and this
// type's single small, infrequent, all-or-nothing snapshot write have
// genuinely different consistency requirements, not a shared one split
// awkwardly across two call sites.
type universeSnapshotDB interface {
	Begin(ctx context.Context) (pgx.Tx, error)
}

// UniverseSnapshotWriter persists one venue's frozen catalog -- snapshot
// provenance plus every instrument's own identity metadata -- atomically,
// idempotently, and fail-closed against a payload mismatch. This is the
// foundation step only (feat/momentum-universe-identity-foundation-v1):
// nothing here compares two venues' own instruments against each other or
// produces a cross-venue match: see momentumsource.Instrument's own doc
// comment on why identity_status has no 'confirmed'/'conflict' value yet.
type UniverseSnapshotWriter struct {
	db universeSnapshotDB
}

func NewUniverseSnapshotWriter(pool *pgxpool.Pool) *UniverseSnapshotWriter {
	return &UniverseSnapshotWriter{db: pool}
}

// PersistCaptureStartupSnapshot wraps PersistUniverseSnapshot with the
// exact capture-startup behavior cmd/momentumcapture and
// cmd/momentumcapturebinance both need identically (a code-review finding:
// the two binaries originally had this block, including its own doc
// comment, copy-pasted verbatim): on failure, close pool and return a
// wrapped error, so the caller's own run() aborts before ever opening a
// trade/ticker stream -- see PersistUniverseSnapshot's own doc comment on
// the capture-startup invariant this exists to enforce. Always uses this
// package's own CaptureVersion (the bar-schema version both binaries
// already share) as provenance; captureVersion is not a parameter here
// because neither binary has ever needed to pass a different one.
func PersistCaptureStartupSnapshot(
	ctx context.Context,
	pool *pgxpool.Pool,
	exchange string,
	universeVersion string,
	instruments []momentumsource.Instrument,
	capturedAt time.Time,
) error {
	writer := NewUniverseSnapshotWriter(pool)
	if err := writer.PersistUniverseSnapshot(ctx, exchange, universeVersion, CaptureVersion, instruments, capturedAt); err != nil {
		pool.Close()
		return fmt.Errorf("persist universe snapshot: %w", err)
	}
	return nil
}

// PersistUniverseSnapshot computes catalog_version and every hash itself
// (never trusts a caller-supplied value for any of them), then writes the
// snapshot row and every instrument row in ONE transaction: per the
// capture-startup invariant this type exists to support (fetch venue
// catalog -> normalize/validate -> freeze the subscription universe ->
// persist that frozen snapshot atomically -> start capture), a
// partially-written snapshot must never be observable -- a caller sees
// either the complete row set or none of it, never bars that reference a
// universe_version with no matching identity catalog at all.
//
// Idempotent: calling this again with byte-identical instruments for the
// same (exchange, universeVersion) is a no-op success (the resulting
// catalogVersion/payloadHash are identical, so the existing row already
// matches). Calling it again with DIFFERENT instruments under what would
// otherwise be the same (exchange, universeVersion, catalogVersion) key --
// only possible if catalogVersion's own hash collided, astronomically
// unlikely, or this function's own hashing changed without a
// schemaVersion bump -- returns ErrSnapshotPayloadMismatch rather than
// overwriting the existing row.
//
// Also idempotent against a genuinely concurrent writer racing for the
// same key (e.g. two capture processes briefly overlapping during a
// redeploy): the check-then-insert below is not by itself atomic across
// transactions, so a second writer can lose a unique_violation race on
// INSERT even though its own payload agrees with the one that just won.
// That case is detected and re-checked against the now-committed row
// rather than surfaced as a raw constraint-violation error.
func (w *UniverseSnapshotWriter) PersistUniverseSnapshot(
	ctx context.Context,
	exchange string,
	universeVersion string,
	captureVersion string,
	instruments []momentumsource.Instrument,
	capturedAt time.Time,
) error {
	sorted := sortedInstruments(instruments)
	catalogVersion, metadataHashes := computeCatalogVersion(sorted)
	payloadHash := computePayloadHash(
		exchange, universeVersion, catalogVersion, len(sorted), sorted, metadataHashes,
	)

	err := w.persistOnce(
		ctx, exchange, universeVersion, catalogVersion, captureVersion, capturedAt, sorted, metadataHashes, payloadHash,
	)
	if !errors.Is(err, errSnapshotInsertRaceLost) {
		return err
	}
	// Lost the race: some other writer's INSERT for this exact key
	// committed between our own SELECT and INSERT. Re-check against what
	// it actually wrote, in a fresh transaction (the one above is aborted
	// by Postgres once a constraint violation occurs and cannot be reused).
	return w.checkAgainstCommitted(ctx, exchange, universeVersion, catalogVersion, payloadHash)
}

func (w *UniverseSnapshotWriter) persistOnce(
	ctx context.Context,
	exchange string,
	universeVersion string,
	catalogVersion string,
	captureVersion string,
	capturedAt time.Time,
	sorted []momentumsource.Instrument,
	metadataHashes [][32]byte,
	payloadHash [32]byte,
) error {
	tx, err := w.db.Begin(ctx)
	if err != nil {
		return fmt.Errorf("begin universe snapshot transaction: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }() // no-op once committed

	existingPayloadHash, found, err := selectExistingPayloadHash(ctx, tx, exchange, universeVersion, catalogVersion)
	if err != nil {
		return err
	}
	if found {
		if payloadHashesEqual(existingPayloadHash, payloadHash) {
			// Already persisted, byte-identical: idempotent success. Roll
			// back (nothing was written this call) rather than commit an
			// empty transaction.
			return nil
		}
		return ErrSnapshotPayloadMismatch
	}

	if _, err := tx.Exec(
		ctx,
		`INSERT INTO app.momentum_universe_snapshots (
			exchange, universe_version, catalog_version, capture_version,
			schema_version, captured_at, instrument_count, payload_hash
		) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)`,
		exchange, universeVersion, catalogVersion, captureVersion,
		IdentitySchemaVersion, capturedAt, len(sorted), payloadHash[:],
	); err != nil {
		if isUniqueViolation(err) {
			// A concurrent writer's own INSERT for this exact key committed
			// between our SELECT above and this INSERT -- not a caller bug
			// (unlike a duplicate native_market_id in the instrument batch
			// below, which is NOT given this treatment). Marked distinctly
			// so PersistUniverseSnapshot can recheck against the row that
			// won, rather than surfacing a raw constraint-violation error.
			return fmt.Errorf("%w: %w", errSnapshotInsertRaceLost, err)
		}
		return fmt.Errorf("insert universe snapshot: %w", err)
	}

	batch := &pgx.Batch{}
	for i, inst := range sorted {
		identityKey, ready := inst.IdentityKey()
		var identityKeyArg *string
		var onboardedAtArg *time.Time
		if ready {
			identityKeyArg = &identityKey
			onboardedAtArg = inst.OnboardedAt
		}
		metadataHash := metadataHashes[i]
		batch.Queue(
			`INSERT INTO app.momentum_universe_instruments (
				exchange, universe_version, catalog_version, native_market_id,
				base, quote, settle, native_market_type, canonical_market_type,
				onboarded_at, identity_status, identity_key, metadata_hash
			) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)`,
			exchange, universeVersion, catalogVersion, inst.NativeMarketID,
			inst.Base, inst.Quote, inst.Settle, inst.NativeMarketType, inst.CanonicalMarketType,
			onboardedAtArg, string(inst.IdentityStatus), identityKeyArg, metadataHash[:],
		)
	}
	results := tx.SendBatch(ctx, batch)
	for range sorted {
		if _, err := results.Exec(); err != nil {
			_ = results.Close()
			return fmt.Errorf("insert universe instrument: %w", err)
		}
	}
	if err := results.Close(); err != nil {
		return fmt.Errorf("close universe instrument batch: %w", err)
	}

	if err := tx.Commit(ctx); err != nil {
		return fmt.Errorf("commit universe snapshot transaction: %w", err)
	}
	return nil
}

// checkAgainstCommitted re-reads the snapshot row after PersistUniverseSnapshot
// lost a unique_violation race on INSERT (see its own doc comment): the row
// that caused the violation has, by definition, already committed, so this
// runs in its own fresh transaction rather than the aborted one.
func (w *UniverseSnapshotWriter) checkAgainstCommitted(
	ctx context.Context,
	exchange string,
	universeVersion string,
	catalogVersion string,
	payloadHash [32]byte,
) error {
	tx, err := w.db.Begin(ctx)
	if err != nil {
		return fmt.Errorf("begin universe snapshot recheck transaction: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()

	existingPayloadHash, found, err := selectExistingPayloadHash(ctx, tx, exchange, universeVersion, catalogVersion)
	if err != nil {
		return err
	}
	if !found {
		// The row that caused our own unique_violation is gone by the time
		// we re-checked (e.g. deleted concurrently) -- genuinely
		// unexpected, surface it rather than guessing at idempotency.
		return fmt.Errorf(
			"momentumcapture: lost a universe snapshot insert race for exchange=%s universe_version=%s catalog_version=%s, but no committed row was found on recheck",
			exchange, universeVersion, catalogVersion,
		)
	}
	if payloadHashesEqual(existingPayloadHash, payloadHash) {
		return nil
	}
	return ErrSnapshotPayloadMismatch
}

func selectExistingPayloadHash(
	ctx context.Context, tx pgx.Tx, exchange, universeVersion, catalogVersion string,
) (existingPayloadHash []byte, found bool, err error) {
	err = tx.QueryRow(
		ctx,
		`SELECT payload_hash FROM app.momentum_universe_snapshots
		 WHERE exchange = $1 AND universe_version = $2 AND catalog_version = $3`,
		exchange, universeVersion, catalogVersion,
	).Scan(&existingPayloadHash)
	switch {
	case err == nil:
		return existingPayloadHash, true, nil
	case errors.Is(err, pgx.ErrNoRows):
		return nil, false, nil
	default:
		return nil, false, fmt.Errorf("check existing universe snapshot: %w", err)
	}
}

func payloadHashesEqual(existing []byte, payloadHash [32]byte) bool {
	return string(existing) == string(payloadHash[:])
}

func isUniqueViolation(err error) bool {
	var pgErr *pgconn.PgError
	return errors.As(err, &pgErr) && pgErr.Code == pgUniqueViolation
}

func sortedInstruments(instruments []momentumsource.Instrument) []momentumsource.Instrument {
	sorted := append([]momentumsource.Instrument(nil), instruments...)
	sort.Slice(sorted, func(i, j int) bool { return sorted[i].NativeMarketID < sorted[j].NativeMarketID })
	return sorted
}

// canonicalInstrumentLine is every field metadata_hash and catalog_version
// are computed from, in a fixed order with a delimiter ("\x1f", ASCII unit
// separator) no real venue field is expected to contain -- unlike "|" or
// ",", which a symbol or asset code could in principle carry.
func canonicalInstrumentLine(inst momentumsource.Instrument) string {
	onboardedAtField := ""
	if inst.OnboardedAt != nil {
		onboardedAtField = strconv.FormatInt(inst.OnboardedAt.UnixMilli(), 10)
	}
	identityKeyField, _ := inst.IdentityKey() // "" when not ready -- the correct, honest value to hash
	return strings.Join([]string{
		inst.Exchange,
		inst.NativeMarketID,
		inst.Base,
		inst.Quote,
		inst.Settle,
		inst.NativeMarketType,
		inst.CanonicalMarketType,
		onboardedAtField,
		string(inst.IdentityStatus),
		identityKeyField,
	}, "\x1f")
}

// computeCatalogVersion hashes IdentitySchemaVersion plus every sorted
// instrument's own canonical line -- see this package's own doc comment on
// why this is a SEPARATE version from universe_version: the same symbol
// SET can stay identical while a symbol's own onboarded_at silently
// changes underneath it, which universe_version (a hash of the symbol list
// alone) would never detect. instruments must already be sorted by
// NativeMarketID (see sortedInstruments) for this to be deterministic
// regardless of catalog fetch order. Each instrument's canonical line is
// built exactly once here (a code-review finding: an earlier version
// built it a second time inside a separate computeMetadataHash) and reused
// for both the catalog-version digest and that instrument's own
// metadata_hash.
//
// The returned hashes are a slice positionally aligned with
// sortedInstruments, not a map keyed by NativeMarketID (a code-review
// finding: a map would silently let two instruments sharing the same
// NativeMarketID overwrite each other's hash -- currently unreachable in
// practice, since app.momentum_universe_instruments' own primary key
// rejects a duplicate native_market_id before any row commits, but the
// hash computation itself should not depend on that downstream constraint
// to be correct). Every caller already iterates the same sortedInstruments
// slice in the same order, so a positional slice needs no key at all.
func computeCatalogVersion(sortedInstruments []momentumsource.Instrument) (string, [][32]byte) {
	lines := make([]string, 0, len(sortedInstruments)+1)
	lines = append(lines, "schema_version="+IdentitySchemaVersion)
	metadataHashes := make([][32]byte, len(sortedInstruments))
	for i, inst := range sortedInstruments {
		line := canonicalInstrumentLine(inst)
		lines = append(lines, line)
		metadataHashes[i] = sha256.Sum256([]byte(line))
	}
	// hashSymbols (universe.go) is this same join-then-sha256-then-hex
	// digest, already used for universe_version -- reused here rather than
	// a second inline copy (a code-review finding).
	return hashSymbols(lines), metadataHashes
}

// computePayloadHash covers everything a repeated write attempt must
// reproduce byte-for-byte to be considered the same snapshot: not just
// catalogVersion's own instrument-content fingerprint, but also the
// snapshot row's own scalar fields (exchange, universe_version,
// instrument_count) and every instrument's own metadata hash. This is
// deliberately a wider net than catalogVersion: a bug that produced the
// right catalogVersion but a wrong instrument_count, for example, must
// still be caught as a mismatch, not silently accepted because the
// identity-relevant hash happened to agree.
//
// captureVersion is deliberately EXCLUDED from this hash (a code-review
// finding): it pins momentum.Bar's own persisted column/histogram shape,
// a concept unrelated to instrument identity, and is bumped independently
// of anything this table cares about. Including it here would have meant
// a routine, identity-unrelated CaptureVersion bump changes payload_hash
// for an otherwise-unchanged catalog on the next restart, which
// PersistUniverseSnapshot treats as ErrSnapshotPayloadMismatch --
// refusing to start the live Bybit capture process over a bar-schema
// change that never touched the instrument catalog at all. captureVersion
// is still stored as pure provenance on the snapshot row (see
// PersistUniverseSnapshot's own INSERT); it is simply not part of what
// makes two write attempts "the same" for idempotency purposes.
// computePayloadHash needs the raw 32 bytes (payload_hash is BYTEA, not a
// hex-encoded VARCHAR like catalog_version), so it cannot reuse hashSymbols
// (universe.go), which returns a hex string -- the underlying join-then-
// sha256 step is otherwise the same computation computeCatalogVersion
// performs via hashSymbols above.
func computePayloadHash(
	exchange string,
	universeVersion string,
	catalogVersion string,
	instrumentCount int,
	sortedInstruments []momentumsource.Instrument,
	metadataHashes [][32]byte,
) [32]byte {
	lines := []string{
		"exchange=" + exchange,
		"universe_version=" + universeVersion,
		"catalog_version=" + catalogVersion,
		"schema_version=" + IdentitySchemaVersion,
		"instrument_count=" + strconv.Itoa(instrumentCount),
	}
	for i, inst := range sortedInstruments {
		hash := metadataHashes[i]
		lines = append(lines, inst.NativeMarketID+"="+hex.EncodeToString(hash[:]))
	}
	return sha256.Sum256([]byte(strings.Join(lines, "\n")))
}
