package vault

import (
	"bytes"
	"testing"
)

func TestCanonicalBytesSortedCompact(t *testing.T) {
	raw, err := CanonicalBytes(map[string]any{
		"b": int64(1),
		"a": map[string]any{"d": int64(1), "c": true},
		"e": []any{nil, false, "café"},
	})
	if err != nil {
		t.Fatal(err)
	}
	want := []byte(`{"a":{"c":true,"d":1},"b":1,"e":[null,false,"café"]}`)
	if !bytes.Equal(raw, want) {
		t.Fatalf("got %s want %s", raw, want)
	}
}

func TestDigestStable(t *testing.T) {
	id := map[string]any{
		"adapter_namespace": "org.example",
		"archive_id":        "archive",
		"identity_scheme":   "native-id",
		"identity_version":  "1",
		"logical_key":       "k",
		"record_type":       "session",
		"source_scope_id":   "scope",
	}
	a, err := EntityID(id)
	if err != nil {
		t.Fatal(err)
	}
	b, err := EntityID(id)
	if err != nil || a != b {
		t.Fatalf("%s %s %v", a, b, err)
	}
	if len(a) != 7+64 || a[:7] != "sha256:" {
		t.Fatalf("digest %s", a)
	}
}

func TestRevisionExcludesImportBookkeeping(t *testing.T) {
	ident := map[string]any{
		"adapter_namespace": "org.example",
		"archive_id":        "archive",
		"identity_scheme":   "native-id",
		"identity_version":  "1",
		"logical_key":       "batch",
		"record_type":       "import_batch",
		"source_scope_id":   "scope",
	}
	eid, err := EntityID(ident)
	if err != nil {
		t.Fatal(err)
	}
	rec := map[string]any{
		"body": map[string]any{
			"archive_id": "archive",
			"batch_id":   "batch",
			"created_at": "2026-01-01T00:00:00Z",
		},
		"entity_id":    eid,
		"identity":     ident,
		"record_type":  "import_batch",
		"sensitivity":  "restricted-unclassified",
		"spec_version": "0.1",
	}
	withClock, err := RevisionID(rec)
	if err != nil {
		t.Fatal(err)
	}
	rec2 := cloneJSON(rec).(map[string]any)
	rec2["body"].(map[string]any)["created_at"] = "2099-01-01T00:00:00Z"
	without, err := RevisionID(rec2)
	if err != nil {
		t.Fatal(err)
	}
	if withClock != without {
		t.Fatalf("bookkeeping changed revision")
	}
}

func TestAlphaLabelPastZ(t *testing.T) {
	if alphaLabel(0) != "A" || alphaLabel(25) != "Z" || alphaLabel(26) != "AA" || alphaLabel(51) != "AZ" {
		t.Fatalf("%s %s %s %s", alphaLabel(0), alphaLabel(25), alphaLabel(26), alphaLabel(51))
	}
}
