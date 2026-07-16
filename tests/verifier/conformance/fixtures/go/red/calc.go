package fx

import "fmt"

func Add(a, b int) int {
	return a + b
}

func Notify() {
	fmt.Sprintf("value: %d", 42)
}
