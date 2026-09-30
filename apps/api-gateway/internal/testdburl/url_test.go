package testdburl

import "testing"

func TestURLAllowsOnlyDisposableLoopbackDatabase(t *testing.T) {
	t.Setenv("DATABASE_URL", "postgresql://schurfer:x@127.0.0.1:15432/production")
	t.Setenv("SCHURFER_TEST_DATABASE_URL", "")
	got, err := URL()
	if err != nil || got != defaultURL {
		t.Fatalf("default test URL = %q, %v", got, err)
	}
	valid := "postgresql://schurfer:x@127.0.0.1:49123/schurfer_verify_abcdef123456"
	t.Setenv("SCHURFER_TEST_DATABASE_URL", valid)
	got, err = URL()
	if err != nil || got != valid {
		t.Fatalf("disposable test URL = %q, %v", got, err)
	}
	for _, invalid := range []string{
		"postgresql://schurfer:x@localhost:5432/schurfer",
		"postgresql://schurfer:x@127.0.0.1:15432/schurfer",
		"postgresql://schurfer:x@127.0.0.1:49123/other_db",
		"postgresql://schurfer:x@db.example.invalid:49123/schurfer_verify_abcdef123456",
		valid + "?hostaddr=db.example.invalid",
		"postgresql://schurfer:x@127.0.0.1:49123",
	} {
		t.Setenv("SCHURFER_TEST_DATABASE_URL", invalid)
		if _, err := URL(); err == nil {
			t.Errorf("unsafe test URL accepted")
		}
	}
}
