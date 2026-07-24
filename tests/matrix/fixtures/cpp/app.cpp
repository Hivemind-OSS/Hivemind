#include <string>

class Base {
public:
    virtual std::string greet() {
        return "base";
    }
};

class Service : public Base {
public:
    std::string name;

    explicit Service(std::string n) : name(n) {}

    std::string run() {
        return helper(name);
    }

    std::string helper(std::string value) {
        return value + "!";
    }
};

int main() {
    Service svc("x");
    svc.run();
    return 0;
}
