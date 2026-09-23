package vault

const (
	defaultMaxFileBytes       = 64 << 20
	hardMaxFileBytes          = 256 << 20
	defaultMaxTotalBytes      = 512 << 20
	hardMaxTotalBytes         = 2 << 30
	defaultMaxOutputBytes     = 512 << 20
	hardMaxOutputBytes        = 2 << 30
	defaultMaxBatches         = 32
	defaultMaxRecordsPerBatch = 20_000
	defaultMaxSessions        = 10_000
	defaultMaxJSONDepth       = 64
	defaultMaxJSONString      = 1 << 20
	defaultMaxWalkDepth       = 8
	maxComponentBytes         = 255
)

// Limits are fail-closed bounds for attested files, JSON, and generated output.
type Limits struct {
	MaxFileBytes       uint64
	MaxTotalBytes      uint64
	MaxOutputBytes     uint64
	MaxBatches         int
	MaxRecordsPerBatch int
	MaxSessions        int
	MaxJSONDepth       int
	MaxJSONStringBytes int
	MaxWalkDepth       int
}

func DefaultLimits() Limits {
	return Limits{
		MaxFileBytes:       defaultMaxFileBytes,
		MaxTotalBytes:      defaultMaxTotalBytes,
		MaxOutputBytes:     defaultMaxOutputBytes,
		MaxBatches:         defaultMaxBatches,
		MaxRecordsPerBatch: defaultMaxRecordsPerBatch,
		MaxSessions:        defaultMaxSessions,
		MaxJSONDepth:       defaultMaxJSONDepth,
		MaxJSONStringBytes: defaultMaxJSONString,
		MaxWalkDepth:       defaultMaxWalkDepth,
	}
}
