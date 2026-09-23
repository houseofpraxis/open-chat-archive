package vault

import (
	"crypto/sha256"
	"encoding/hex"
	"os"
	"path/filepath"
	"regexp"
)

var (
	opaqueRe   = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`)
	digestRe   = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)
	batchRelRe = regexp.MustCompile(`^batches/[A-Za-z0-9][A-Za-z0-9._-]{0,127}/(manifest\.json|records\.ndjson)$`)
)

type fileAttest struct {
	Path       string
	Size       int64
	ByteDigest string
	Count      int64 // records only; -1 for manifest
}

type batchAttest struct {
	BatchID  string
	Manifest fileAttest
	Records  fileAttest
}

type Attestation struct {
	ArchiveID        string
	ValidatorVersion string
	Batches          []batchAttest
}

func exactKeys(m map[string]any, keys ...string) error {
	if len(m) != len(keys) {
		return errc(AttestationInvalid, 1)
	}
	for _, k := range keys {
		if _, ok := m[k]; !ok {
			return errc(AttestationInvalid, 1)
		}
	}
	return nil
}

func parseAttestation(raw []byte, lim Limits) (*Attestation, error) {
	v, err := ParseJSON(raw, lim)
	if err != nil {
		return nil, err
	}
	obj, ok := asObject(v)
	if !ok {
		return nil, errc(AttestationInvalid, 1)
	}
	if err := exactKeys(obj, "archive_id", "attestation_kind", "batches", "format", "spec_version", "status", "validator"); err != nil {
		return nil, err
	}
	format, _ := asString(obj["format"])
	spec, _ := asString(obj["spec_version"])
	kind, _ := asString(obj["attestation_kind"])
	status, _ := asString(obj["status"])
	archiveID, _ := asString(obj["archive_id"])
	if format != FormatAttestation || spec != SpecVersion || kind != AttestationKind || status != StatusPassed {
		return nil, errc(AttestationInvalid, 1)
	}
	if !opaqueRe.MatchString(archiveID) {
		return nil, errc(AttestationInvalid, 1)
	}
	valObj, ok := asObject(obj["validator"])
	if !ok {
		return nil, errc(AttestationInvalid, 1)
	}
	if err := exactKeys(valObj, "name", "package_version"); err != nil {
		return nil, err
	}
	name, _ := asString(valObj["name"])
	pkgVer, _ := asString(valObj["package_version"])
	if name != ValidatorName || pkgVer == "" {
		return nil, errc(AttestationInvalid, 1)
	}
	arr, ok := asArray(obj["batches"])
	if !ok || len(arr) == 0 {
		return nil, errc(AttestationInvalid, 1)
	}
	if len(arr) > lim.MaxBatches {
		return nil, errc(LimitExceeded, 1)
	}
	seen := map[string]struct{}{}
	batches := make([]batchAttest, 0, len(arr))
	var prev string
	for i, item := range arr {
		b, err := parseBatchAttest(item)
		if err != nil {
			return nil, err
		}
		if _, ok := seen[b.BatchID]; ok {
			return nil, errc(AttestationInvalid, 1)
		}
		seen[b.BatchID] = struct{}{}
		if i > 0 && b.BatchID <= prev {
			return nil, errc(AttestationInvalid, 1)
		}
		prev = b.BatchID
		batches = append(batches, b)
	}
	return &Attestation{ArchiveID: archiveID, ValidatorVersion: pkgVer, Batches: batches}, nil
}

func parseBatchAttest(v any) (batchAttest, error) {
	obj, ok := asObject(v)
	if !ok {
		return batchAttest{}, errc(AttestationInvalid, 1)
	}
	if err := exactKeys(obj, "batch_id", "manifest", "records"); err != nil {
		return batchAttest{}, err
	}
	id, _ := asString(obj["batch_id"])
	if !opaqueRe.MatchString(id) {
		return batchAttest{}, errc(AttestationInvalid, 1)
	}
	man, err := parseFileAttest(obj["manifest"], false)
	if err != nil {
		return batchAttest{}, err
	}
	rec, err := parseFileAttest(obj["records"], true)
	if err != nil {
		return batchAttest{}, err
	}
	wantMan := "batches/" + id + "/manifest.json"
	wantRec := "batches/" + id + "/records.ndjson"
	if man.Path != wantMan || rec.Path != wantRec {
		return batchAttest{}, errc(AttestationInvalid, 1)
	}
	if !batchRelRe.MatchString(man.Path) || !batchRelRe.MatchString(rec.Path) {
		return batchAttest{}, errc(PathAllowlist, 1)
	}
	return batchAttest{BatchID: id, Manifest: man, Records: rec}, nil
}

func parseFileAttest(v any, withCount bool) (fileAttest, error) {
	obj, ok := asObject(v)
	if !ok {
		return fileAttest{}, errc(AttestationInvalid, 1)
	}
	if withCount {
		if err := exactKeys(obj, "byte_digest", "count", "path", "size"); err != nil {
			return fileAttest{}, err
		}
	} else {
		if err := exactKeys(obj, "byte_digest", "path", "size"); err != nil {
			return fileAttest{}, err
		}
	}
	path, _ := asString(obj["path"])
	digest, _ := asString(obj["byte_digest"])
	size, ok := asInt(obj["size"])
	if !ok || size < 0 || path == "" || !digestRe.MatchString(digest) {
		return fileAttest{}, errc(AttestationInvalid, 1)
	}
	fa := fileAttest{Path: path, Size: size, ByteDigest: digest, Count: -1}
	if withCount {
		count, ok := asInt(obj["count"])
		if !ok || count < 0 {
			return fileAttest{}, errc(AttestationInvalid, 1)
		}
		fa.Count = count
	}
	return fa, nil
}

func loadAttestationFile(path string, lim Limits) (*Attestation, error) {
	raw, err := openRegularNoFollow(path, lim.MaxFileBytes)
	if err != nil {
		return nil, err
	}
	return parseAttestation(raw, lim)
}

func sha256Digest(data []byte) string {
	sum := sha256.Sum256(data)
	return "sha256:" + hex.EncodeToString(sum[:])
}

func reconcileBatches(archiveRoot string, att *Attestation, lim Limits) error {
	names, err := readDirNamesNoFollow(filepath.Join(archiveRoot, "batches"))
	if err != nil {
		return err
	}
	onDisk := map[string]struct{}{}
	for _, name := range names {
		if name == "." || name == ".." {
			continue
		}
		p := filepath.Join(archiveRoot, "batches", name)
		fi, err := lstatNoFollow(p)
		if err != nil {
			return err
		}
		if fi.Mode()&os.ModeSymlink != 0 {
			return errc(RefuseSymlink, 3)
		}
		if !fi.IsDir() {
			return errc(BatchSetMismatch, 1)
		}
		if !opaqueRe.MatchString(name) {
			return errc(BatchSetMismatch, 1)
		}
		onDisk[name] = struct{}{}
	}
	if len(onDisk) != len(att.Batches) {
		return errc(BatchSetMismatch, 1)
	}
	for _, b := range att.Batches {
		if _, ok := onDisk[b.BatchID]; !ok {
			return errc(BatchSetMismatch, 1)
		}
	}
	_ = lim
	return nil
}
