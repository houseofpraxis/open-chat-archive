package vault

import (
	"bytes"
	"unicode/utf8"
)

func ParseJSON(raw []byte, lim Limits) (any, error) {
	if bytes.HasPrefix(raw, []byte{0xEF, 0xBB, 0xBF}) {
		return nil, errc(JSONBOM, 1)
	}
	if bytes.IndexByte(raw, '\r') >= 0 {
		return nil, errc(JSONNewline, 1)
	}
	p := parser{s: raw, lim: lim}
	v, err := p.parseValue()
	if err != nil {
		return nil, err
	}
	p.skipSpace()
	if p.i != len(p.s) {
		return nil, errc(JSONSyntax, 1)
	}
	return v, nil
}

func ParseNDJSON(raw []byte, lim Limits) ([]any, error) {
	if bytes.HasPrefix(raw, []byte{0xEF, 0xBB, 0xBF}) {
		return nil, errc(JSONBOM, 1)
	}
	if len(raw) == 0 || raw[len(raw)-1] != '\n' || bytes.IndexByte(raw, '\r') >= 0 {
		return nil, errc(JSONNewline, 1)
	}
	lines := bytes.Split(raw[:len(raw)-1], []byte{'\n'})
	if len(lines) > lim.MaxRecordsPerBatch {
		return nil, errc(LimitExceeded, 1)
	}
	out := make([]any, 0, len(lines))
	for _, line := range lines {
		if len(line) == 0 {
			return nil, errc(JSONBlankLine, 1)
		}
		v, err := ParseJSON(line, lim)
		if err != nil {
			return nil, err
		}
		out = append(out, v)
	}
	return out, nil
}

type parser struct {
	s     []byte
	i     int
	depth int
	lim   Limits
}

func (p *parser) skipSpace() {
	for p.i < len(p.s) {
		c := p.s[p.i]
		if c != ' ' && c != '\t' && c != '\n' {
			return
		}
		p.i++
	}
}

func (p *parser) parseValue() (any, error) {
	p.skipSpace()
	if p.i >= len(p.s) {
		return nil, errc(JSONSyntax, 1)
	}
	switch p.s[p.i] {
	case '{':
		return p.parseObject()
	case '[':
		return p.parseArray()
	case '"':
		return p.parseString()
	case 't':
		return p.parseLiteral("true", true)
	case 'f':
		return p.parseLiteral("false", false)
	case 'n':
		return p.parseLiteral("null", nil)
	case '-', '0', '1', '2', '3', '4', '5', '6', '7', '8', '9':
		return p.parseNumber()
	default:
		return nil, errc(JSONSyntax, 1)
	}
}

func (p *parser) parseLiteral(lit string, v any) (any, error) {
	if p.i+len(lit) > len(p.s) || string(p.s[p.i:p.i+len(lit)]) != lit {
		return nil, errc(JSONSyntax, 1)
	}
	p.i += len(lit)
	return v, nil
}

func (p *parser) parseObject() (any, error) {
	p.depth++
	if p.depth > p.lim.MaxJSONDepth {
		return nil, errc(JSONDepth, 1)
	}
	p.i++ // '{'
	p.skipSpace()
	obj := map[string]any{}
	if p.i < len(p.s) && p.s[p.i] == '}' {
		p.i++
		p.depth--
		return obj, nil
	}
	for {
		p.skipSpace()
		if p.i >= len(p.s) || p.s[p.i] != '"' {
			return nil, errc(JSONSyntax, 1)
		}
		key, err := p.parseString()
		if err != nil {
			return nil, err
		}
		if _, exists := obj[key]; exists {
			return nil, errc(JSONDuplicateKey, 1)
		}
		p.skipSpace()
		if p.i >= len(p.s) || p.s[p.i] != ':' {
			return nil, errc(JSONSyntax, 1)
		}
		p.i++
		val, err := p.parseValue()
		if err != nil {
			return nil, err
		}
		obj[key] = val
		p.skipSpace()
		if p.i >= len(p.s) {
			return nil, errc(JSONSyntax, 1)
		}
		if p.s[p.i] == '}' {
			p.i++
			p.depth--
			return obj, nil
		}
		if p.s[p.i] != ',' {
			return nil, errc(JSONSyntax, 1)
		}
		p.i++
	}
}

func (p *parser) parseArray() (any, error) {
	p.depth++
	if p.depth > p.lim.MaxJSONDepth {
		return nil, errc(JSONDepth, 1)
	}
	p.i++ // '['
	p.skipSpace()
	arr := []any{}
	if p.i < len(p.s) && p.s[p.i] == ']' {
		p.i++
		p.depth--
		return arr, nil
	}
	for {
		val, err := p.parseValue()
		if err != nil {
			return nil, err
		}
		arr = append(arr, val)
		p.skipSpace()
		if p.i >= len(p.s) {
			return nil, errc(JSONSyntax, 1)
		}
		if p.s[p.i] == ']' {
			p.i++
			p.depth--
			return arr, nil
		}
		if p.s[p.i] != ',' {
			return nil, errc(JSONSyntax, 1)
		}
		p.i++
	}
}

func (p *parser) parseNumber() (any, error) {
	start := p.i
	if p.s[p.i] == '-' {
		p.i++
		if p.i >= len(p.s) {
			return nil, errc(JSONSyntax, 1)
		}
	}
	if p.i >= len(p.s) || p.s[p.i] < '0' || p.s[p.i] > '9' {
		return nil, errc(JSONSyntax, 1)
	}
	if p.s[p.i] == '0' {
		p.i++
		if p.i < len(p.s) && p.s[p.i] >= '0' && p.s[p.i] <= '9' {
			return nil, errc(JSONSyntax, 1)
		}
	} else {
		for p.i < len(p.s) && p.s[p.i] >= '0' && p.s[p.i] <= '9' {
			p.i++
		}
	}
	if p.i < len(p.s) && (p.s[p.i] == '.' || p.s[p.i] == 'e' || p.s[p.i] == 'E') {
		return nil, errc(JSONFloat, 1)
	}
	tok := p.s[start:p.i]
	if len(tok) == 0 {
		return nil, errc(JSONSyntax, 1)
	}
	if len(tok) > p.lim.MaxJSONStringBytes {
		return nil, errc(LimitExceeded, 1)
	}
	// Normalize JSON -0 to 0 so canonical bytes match Python json.dumps.
	if len(tok) == 2 && tok[0] == '-' && tok[1] == '0' {
		return jsonInt("0"), nil
	}
	return jsonInt(tok), nil
}

func (p *parser) parseString() (string, error) {
	p.i++ // '"'
	var buf []byte
	for p.i < len(p.s) {
		c := p.s[p.i]
		if c == '"' {
			p.i++
			if !utf8.Valid(buf) {
				return "", errc(JSONUTF8, 1)
			}
			s := string(buf)
			if hasSurrogate(s) {
				return "", errc(JSONUnicode, 1)
			}
			if len(s) > p.lim.MaxJSONStringBytes {
				return "", errc(LimitExceeded, 1)
			}
			return s, nil
		}
		if c == '\\' {
			p.i++
			if p.i >= len(p.s) {
				return "", errc(JSONSyntax, 1)
			}
			esc, n, err := p.parseEscape()
			if err != nil {
				return "", err
			}
			buf = append(buf, esc[:n]...)
			continue
		}
		if c < 0x20 {
			return "", errc(JSONSyntax, 1)
		}
		if c < 0x80 {
			buf = append(buf, c)
			p.i++
			continue
		}
		r, size := utf8.DecodeRune(p.s[p.i:])
		if r == utf8.RuneError && size == 1 {
			return "", errc(JSONUTF8, 1)
		}
		buf = append(buf, p.s[p.i:p.i+size]...)
		p.i += size
	}
	return "", errc(JSONSyntax, 1)
}

func (p *parser) parseEscape() (out [4]byte, n int, err error) {
	c := p.s[p.i]
	p.i++
	switch c {
	case '"', '\\', '/':
		out[0] = c
		return out, 1, nil
	case 'b':
		out[0] = '\b'
		return out, 1, nil
	case 'f':
		out[0] = '\f'
		return out, 1, nil
	case 'n':
		out[0] = '\n'
		return out, 1, nil
	case 'r':
		out[0] = '\r'
		return out, 1, nil
	case 't':
		out[0] = '\t'
		return out, 1, nil
	case 'u':
		r, err := p.parseHexRune()
		if err != nil {
			return out, 0, err
		}
		if r >= 0xD800 && r <= 0xDBFF {
			if p.i+1 < len(p.s) && p.s[p.i] == '\\' && p.s[p.i+1] == 'u' {
				p.i += 2
				r2, err := p.parseHexRune()
				if err != nil {
					return out, 0, err
				}
				if r2 < 0xDC00 || r2 > 0xDFFF {
					return out, 0, errc(JSONUnicode, 1)
				}
				r = 0x10000 + (r-0xD800)<<10 + (r2 - 0xDC00)
			} else {
				return out, 0, errc(JSONUnicode, 1)
			}
		} else if r >= 0xDC00 && r <= 0xDFFF {
			return out, 0, errc(JSONUnicode, 1)
		}
		n = utf8.EncodeRune(out[:], r)
		return out, n, nil
	default:
		return out, 0, errc(JSONSyntax, 1)
	}
}

func (p *parser) parseHexRune() (rune, error) {
	if p.i+4 > len(p.s) {
		return 0, errc(JSONSyntax, 1)
	}
	var r rune
	for k := 0; k < 4; k++ {
		c := p.s[p.i+k]
		var d rune
		switch {
		case c >= '0' && c <= '9':
			d = rune(c - '0')
		case c >= 'a' && c <= 'f':
			d = rune(c - 'a' + 10)
		case c >= 'A' && c <= 'F':
			d = rune(c - 'A' + 10)
		default:
			return 0, errc(JSONSyntax, 1)
		}
		r = r<<4 | d
	}
	p.i += 4
	return r, nil
}

func hasSurrogate(s string) bool {
	for _, r := range s {
		if r >= 0xD800 && r <= 0xDFFF {
			return true
		}
	}
	return false
}
