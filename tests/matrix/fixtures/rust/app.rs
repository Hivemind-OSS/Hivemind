use std::fmt;

trait Greet {
    fn greet(&self) -> String;
}

struct Service {
    name: String,
}

impl Service {
    fn new(name: String) -> Service {
        Service { name }
    }

    fn run(&self) -> String {
        helper(&self.name)
    }
}

impl Greet for Service {
    fn greet(&self) -> String {
        String::from("hi")
    }
}

fn helper(value: &str) -> String {
    value.to_uppercase()
}

fn main() {
    let svc = Service::new(String::from("x"));
    println!("{}", svc.run());
}
