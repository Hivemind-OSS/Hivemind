package main

import "fmt"

type Greeter struct {
	Name string
}

func (g Greeter) Run() string {
	return helper(g.Name)
}

func helper(value string) string {
	return fmt.Sprintf("%s!", value)
}

func main() {
	g := Greeter{Name: "x"}
	fmt.Println(g.Run())
}
