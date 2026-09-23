package main

import (
	"os"

	"github.com/houseofpraxis/open-chat-archive/toolkit-go/internal/vault"
)

func main() {
	os.Exit(vault.Main(os.Args[1:], os.Stdout, os.Stderr))
}
