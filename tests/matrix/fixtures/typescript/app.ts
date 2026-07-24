import { helper } from './util';

interface Named {
  name: string;
}

class Service implements Named {
  name: string;

  constructor(name: string) {
    this.name = name;
  }

  run(): string {
    return helper(this.name);
  }
}

function main(): string {
  const svc = new Service('x');
  return svc.run();
}

export { Service, main };
