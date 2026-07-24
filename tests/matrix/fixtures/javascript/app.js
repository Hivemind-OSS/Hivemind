import { helper } from './util.js';

class Base {
  greet() {
    return 'base';
  }
}

class Service extends Base {
  constructor(name) {
    super();
    this.name = name;
  }

  run() {
    return helper(this.name);
  }
}

function main() {
  const svc = new Service('x');
  return svc.run();
}

export { Service, main };
