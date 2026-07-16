package fx

import "testing"

func TestAdd(t *testing.T) {
	if Add(2, 2) != 4 {
		t.Fatalf("Add(2,2) = %d, want 4", Add(2, 2))
	}
}
