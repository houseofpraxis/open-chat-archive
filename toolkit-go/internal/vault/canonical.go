package vault

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"math/big"
	"sort"
	"strconv"
)

// jsonInt is a canonical JSON integer token (no leading zeros except 0).
// Python json.loads uses unbounded int; identity hashing must emit the same
// decimal. This is not a schema bound.
type jsonInt string

var importBookkeeping = map[string]struct{}{
	"batch_id": {}, "created_at": {}, "imported_at": {}, "import_host": {}, "run_id": {},
}

func CanonicalBytes(v any) ([]byte, error) {
	var buf bytes.Buffer
	if err := encodeJSON(&buf, v, false); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

// FrontmatterBytes encodes v as compact JSON with YAML/Unicode line-break
// escapes. Identity hashing continues to use CanonicalBytes.
func FrontmatterBytes(v any) ([]byte, error) {
	var buf bytes.Buffer
	if err := encodeJSON(&buf, v, true); err != nil {
		return nil, err
	}
	raw := buf.Bytes()
	if hasPhysicalLineBreak(raw) {
		return nil, errc(RecordInvalid, 1)
	}
	return raw, nil
}

func hasPhysicalLineBreak(raw []byte) bool {
	if bytes.IndexByte(raw, '\n') >= 0 || bytes.IndexByte(raw, '\r') >= 0 ||
		bytes.IndexByte(raw, 0x0b) >= 0 || bytes.IndexByte(raw, 0x0c) >= 0 {
		return true
	}
	if bytes.Contains(raw, []byte{0xC2, 0x85}) { // U+0085 NEL
		return true
	}
	if bytes.Contains(raw, []byte{0xE2, 0x80, 0xA8}) { // U+2028
		return true
	}
	if bytes.Contains(raw, []byte{0xE2, 0x80, 0xA9}) { // U+2029
		return true
	}
	return false
}

func Digest(v any) (string, error) {
	b, err := CanonicalBytes(v)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(b)
	return "sha256:" + hex.EncodeToString(sum[:]), nil
}

func EntityID(identity map[string]any) (string, error) {
	return Digest(identity)
}

func RevisionID(record map[string]any) (string, error) {
	return Digest(semanticProjection(record))
}

func semanticProjection(record map[string]any) map[string]any {
	projected := map[string]any{}
	for _, k := range []string{"spec_version", "record_type", "entity_id", "sensitivity", "identity", "body", "extensions"} {
		if v, ok := record[k]; ok {
			projected[k] = cloneJSON(v)
		}
	}
	body, ok := projected["body"].(map[string]any)
	if !ok {
		return projected
	}
	rt, _ := record["record_type"].(string)
	switch rt {
	case "source_artifact", "artifact":
		delete(body, "storage_path")
	case "import_batch":
		for k := range importBookkeeping {
			delete(body, k)
		}
	}
	return projected
}

func cloneJSON(v any) any {
	switch t := v.(type) {
	case map[string]any:
		out := make(map[string]any, len(t))
		for k, child := range t {
			out[k] = cloneJSON(child)
		}
		return out
	case []any:
		out := make([]any, len(t))
		for i, child := range t {
			out[i] = cloneJSON(child)
		}
		return out
	default:
		return v
	}
}

func encodeJSON(buf *bytes.Buffer, v any, yamlSafe bool) error {
	switch t := v.(type) {
	case nil:
		buf.WriteString("null")
		return nil
	case bool:
		if t {
			buf.WriteString("true")
		} else {
			buf.WriteString("false")
		}
		return nil
	case jsonInt:
		buf.WriteString(string(t))
		return nil
	case int64:
		buf.WriteString(strconv.FormatInt(t, 10))
		return nil
	case int:
		buf.WriteString(strconv.FormatInt(int64(t), 10))
		return nil
	case *big.Int:
		if t == nil {
			return errc(JSONSyntax, 1)
		}
		buf.WriteString(t.Text(10))
		return nil
	case string:
		writeJSONString(buf, t, yamlSafe)
		return nil
	case []any:
		buf.WriteByte('[')
		for i, item := range t {
			if i > 0 {
				buf.WriteByte(',')
			}
			if err := encodeJSON(buf, item, yamlSafe); err != nil {
				return err
			}
		}
		buf.WriteByte(']')
		return nil
	case map[string]any:
		keys := make([]string, 0, len(t))
		for k := range t {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		buf.WriteByte('{')
		for i, k := range keys {
			if i > 0 {
				buf.WriteByte(',')
			}
			writeJSONString(buf, k, yamlSafe)
			buf.WriteByte(':')
			if err := encodeJSON(buf, t[k], yamlSafe); err != nil {
				return err
			}
		}
		buf.WriteByte('}')
		return nil
	default:
		return errc(JSONSyntax, 1)
	}
}

func writeJSONString(buf *bytes.Buffer, s string, yamlSafe bool) {
	buf.WriteByte('"')
	for _, r := range s {
		switch r {
		case '"':
			buf.WriteString(`\"`)
		case '\\':
			buf.WriteString(`\\`)
		case '\b':
			buf.WriteString(`\b`)
		case '\f':
			buf.WriteString(`\f`)
		case '\n':
			buf.WriteString(`\n`)
		case '\r':
			buf.WriteString(`\r`)
		case '\t':
			buf.WriteString(`\t`)
		default:
			if r < 0x20 {
				fmt.Fprintf(buf, `\u%04x`, r)
			} else if yamlSafe && (r == 0x85 || r == 0x2028 || r == 0x2029) {
				fmt.Fprintf(buf, `\u%04x`, r)
			} else {
				buf.WriteRune(r)
			}
		}
	}
	buf.WriteByte('"')
}

func asObject(v any) (map[string]any, bool) {
	m, ok := v.(map[string]any)
	return m, ok
}

func asString(v any) (string, bool) {
	s, ok := v.(string)
	return s, ok
}

func asInt(v any) (int64, bool) {
	switch t := v.(type) {
	case int64:
		return t, true
	case int:
		return int64(t), true
	case jsonInt:
		n, err := strconv.ParseInt(string(t), 10, 64)
		if err != nil {
			return 0, false
		}
		return n, true
	case *big.Int:
		if t == nil || !t.IsInt64() {
			return 0, false
		}
		return t.Int64(), true
	default:
		return 0, false
	}
}

func jsonIntValue(v any) (*big.Int, bool) {
	switch t := v.(type) {
	case jsonInt:
		if string(t) == "" {
			return nil, false
		}
		n := new(big.Int)
		if _, ok := n.SetString(string(t), 10); !ok {
			return nil, false
		}
		return n, true
	case int64:
		return big.NewInt(t), true
	case int:
		return big.NewInt(int64(t)), true
	case *big.Int:
		if t == nil {
			return nil, false
		}
		return new(big.Int).Set(t), true
	default:
		return nil, false
	}
}

func jsonIntLex(v any) (string, bool) {
	n, ok := jsonIntValue(v)
	if !ok {
		return "", false
	}
	return n.Text(10), true
}

func asArray(v any) ([]any, bool) {
	a, ok := v.([]any)
	return a, ok
}

func objectString(m map[string]any, key string) (string, bool) {
	if m == nil {
		return "", false
	}
	s, ok := m[key].(string)
	return s, ok && s != ""
}

func objectStringOrEmpty(m map[string]any, key string) string {
	if m == nil {
		return ""
	}
	s, _ := m[key].(string)
	return s
}
