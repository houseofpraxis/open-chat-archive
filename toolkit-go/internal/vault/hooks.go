package vault

// testHook holds optional fault-injection callbacks used only by tests in this
// package. Production CLI flags and environment never read these fields.
var testHook struct {
	failRenameStaging bool
	failRestore       bool
	failDirSync       bool
}

func resetTestHook() {
	testHook.failRenameStaging = false
	testHook.failRestore = false
	testHook.failDirSync = false
}
