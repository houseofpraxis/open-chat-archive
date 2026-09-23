package vault

import (
	"strings"
	"testing"
)

func TestParseJSONRejectsBOMDuplicateFloatCRLF(t *testing.T) {
	lim := DefaultLimits()
	if _, err := ParseJSON([]byte("\xef\xbb\xbf{}"), lim); ErrorCode(err) != JSONBOM {
		t.Fatalf("bom: %v", err)
	}
	if _, err := ParseJSON([]byte("{\"a\":1,\"a\":2}"), lim); ErrorCode(err) != JSONDuplicateKey {
		t.Fatalf("dup: %v", err)
	}
	if _, err := ParseJSON([]byte("{\"n\":1.5}"), lim); ErrorCode(err) != JSONFloat {
		t.Fatalf("float: %v", err)
	}
	if _, err := ParseJSON([]byte("{\"n\":1e2}"), lim); ErrorCode(err) != JSONFloat {
		t.Fatalf("exp: %v", err)
	}
	if _, err := ParseJSON([]byte("{\r\n}"), lim); ErrorCode(err) != JSONNewline {
		t.Fatalf("crlf: %v", err)
	}
	if _, err := ParseJSON([]byte("{\"k\":\"\\uD800\"}"), lim); ErrorCode(err) != JSONUnicode {
		t.Fatalf("surrogate: %v", err)
	}
	v, err := ParseJSON([]byte(" {\"z\":true,\"a\":[null,false,0,-1]} \n"), lim)
	if err != nil {
		t.Fatal(err)
	}
	obj := v.(map[string]any)
	if obj["z"] != true {
		t.Fatalf("true: %#v", obj["z"])
	}
	arr := obj["a"].([]any)
	z, zok := asInt(arr[2])
	n, nok := asInt(arr[3])
	if !zok || !nok || z != 0 || n != -1 {
		t.Fatalf("ints %#v", arr)
	}
}

func TestParseNDJSONRejectsBlankAndUnterminated(t *testing.T) {
	lim := DefaultLimits()
	if _, err := ParseNDJSON([]byte("{\"a\":1}"), lim); ErrorCode(err) != JSONNewline {
		t.Fatalf("unterminated: %v", err)
	}
	if _, err := ParseNDJSON([]byte("{\"a\":1}\n\n"), lim); ErrorCode(err) != JSONBlankLine {
		t.Fatalf("blank: %v", err)
	}
	got, err := ParseNDJSON([]byte("{\"a\":1}\n{\"b\":2}\n"), lim)
	if err != nil || len(got) != 2 {
		t.Fatalf("ok: %v %#v", err, got)
	}
}

func TestParseJSONDepth(t *testing.T) {
	lim := DefaultLimits()
	lim.MaxJSONDepth = 2
	if _, err := ParseJSON([]byte("{\"a\":{\"b\":{\"c\":1}}}"), lim); ErrorCode(err) != JSONDepth {
		t.Fatalf("depth: %v", err)
	}
}

func TestParseJSONInvalidUTF8(t *testing.T) {
	lim := DefaultLimits()
	raw := []byte("{\"k\":\"")
	raw = append(raw, 0xff)
	raw = append(raw, "\"}"...)
	if _, err := ParseJSON(raw, lim); ErrorCode(err) != JSONUTF8 {
		t.Fatalf("utf8: %v", err)
	}
}

func TestParseJSONSurrogatePair(t *testing.T) {
	lim := DefaultLimits()
	v, err := ParseJSON([]byte("\"\\uD83D\\uDE00\""), lim)
	if err != nil {
		t.Fatal(err)
	}
	if v.(string) != "😀" {
		t.Fatalf("got %q", v)
	}
}

func TestFenceLongerThanContent(t *testing.T) {
	content := strings.Repeat("`", 10) + " inner"
	got := fence("text", content)
	if !strings.HasPrefix(got, strings.Repeat("`", 11)+"text\n") {
		t.Fatalf("fence %q", got[:20])
	}
	if strings.Contains(got, "\n"+strings.Repeat("`", 10)+"\n") {
		t.Fatal("content fence must not close the wrapper")
	}
}
