package main

import (
	"testing"
	"time"
)

func TestOptionsAreBoundedToTheProtocol(t *testing.T) {
	o, err := parseOptions(nil)
	if err != nil || o.duration != 28*24*time.Hour || o.maxOpen != 3 || o.hold != time.Hour {
		t.Fatalf("defaults %+v %v", o, err)
	}
	for _, args := range [][]string{
		{"-duration", "700h"}, {"-duration", "0s"}, {"-max-open", "0"}, {"-hold", "0s"},
	} {
		if _, err := parseOptions(args); err == nil {
			t.Fatalf("%v must be refused", args)
		}
	}
}
