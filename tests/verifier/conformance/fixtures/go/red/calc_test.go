package fx

import "testing"

func TestAdd(t *testing.T) {
	if Add(2, 2) != 5 {
		t.Fatalf("Add(2,2) = %d, want 5", Add(2, 2))
	}
}
