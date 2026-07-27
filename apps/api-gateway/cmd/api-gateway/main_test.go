package main

import (
	"context"
	"testing"

	"github.com/alicebob/miniredis/v2"
	"github.com/redis/go-redis/v9"
)

func TestLoadSignalBasesPrefersMeasurementFeed(t *testing.T) {
	server := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: server.Addr()})
	t.Cleanup(func() { _ = client.Close() })
	ctx := context.Background()

	if err := client.Set(
		ctx,
		publicPumpsKey,
		`{"pumps":[{"base":"PUBLIC"}]}`,
		0,
	).Err(); err != nil {
		t.Fatal(err)
	}
	if err := client.Set(
		ctx,
		measurementPumpsKey,
		`{"pumps":[{"base":"MEASURED"},{"base":""}]}`,
		0,
	).Err(); err != nil {
		t.Fatal(err)
	}

	bases, err := loadSignalBases(ctx, client)
	if err != nil {
		t.Fatal(err)
	}
	if len(bases) != 1 || bases[0] != "MEASURED" {
		t.Fatalf("unexpected bases: %v", bases)
	}
}

func TestLoadSignalBasesFallsBackToPublicFeed(t *testing.T) {
	server := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: server.Addr()})
	t.Cleanup(func() { _ = client.Close() })
	ctx := context.Background()

	if err := client.Set(
		ctx,
		publicPumpsKey,
		`{"pumps":[{"base":"LEGACY"}]}`,
		0,
	).Err(); err != nil {
		t.Fatal(err)
	}

	bases, err := loadSignalBases(ctx, client)
	if err != nil {
		t.Fatal(err)
	}
	if len(bases) != 1 || bases[0] != "LEGACY" {
		t.Fatalf("unexpected bases: %v", bases)
	}
}

func TestLoadSignalBasesDoesNotMaskInvalidMeasurementFeed(t *testing.T) {
	server := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: server.Addr()})
	t.Cleanup(func() { _ = client.Close() })
	ctx := context.Background()

	if err := client.Set(ctx, publicPumpsKey, `{"pumps":[{"base":"PUBLIC"}]}`, 0).Err(); err != nil {
		t.Fatal(err)
	}
	if err := client.Set(ctx, measurementPumpsKey, `{`, 0).Err(); err != nil {
		t.Fatal(err)
	}

	if _, err := loadSignalBases(ctx, client); err == nil {
		t.Fatal("expected invalid measurement feed to fail closed")
	}
}
