// Ambient declarations for exactly the slice of the Node standard library this
// harness touches. Hand-written on purpose: the dev-dependency budget is one
// package (the compiler), so the type surface of the runtime is declared here
// rather than pulled in. Narrow by construction — anything not declared below is
// a compile error, which is a second, cheaper guard on how much runtime the
// harness is allowed to reach for.

declare module "node:fs" {
  export function existsSync(path: string): boolean
  export function readFileSync(path: string, encoding: "utf8"): string
  export function writeFileSync(path: string, data: string, encoding: "utf8"): void
  export function mkdirSync(path: string, options: { recursive: true }): void
  export function renameSync(from: string, to: string): void
  export function unlinkSync(path: string): void
  export function rmSync(path: string, options: { recursive: true; force: true }): void
  export function cpSync(from: string, to: string, options: { recursive: true }): void
  export function readdirSync(path: string): string[]
  export function mkdtempSync(prefix: string): string
  export function statSync(path: string): { mtimeMs: number; isDirectory(): boolean }
}

declare module "node:path" {
  export function join(...parts: string[]): string
  export function dirname(path: string): string
  export function basename(path: string): string
  export function resolve(...parts: string[]): string
  export function relative(from: string, to: string): string
  export function isAbsolute(path: string): boolean
  export const sep: string
}

declare module "node:os" {
  export function tmpdir(): string
  export function homedir(): string
  export function platform(): string
}

declare module "node:crypto" {
  export interface Hash {
    update(data: string): Hash
    digest(encoding: "hex"): string
  }
  export function createHash(algorithm: string): Hash
}

declare module "node:url" {
  export function fileURLToPath(url: string | URL): string
}

declare module "node:test" {
  export function test(name: string, fn: () => void | Promise<void>): void
  export function test(
    name: string,
    options: { skip?: boolean | string },
    fn: () => void | Promise<void>,
  ): void
}

declare module "node:assert/strict" {
  interface Assert {
    (value: unknown, message?: string): void
    ok(value: unknown, message?: string): void
    equal(actual: unknown, expected: unknown, message?: string): void
    notEqual(actual: unknown, expected: unknown, message?: string): void
    deepEqual(actual: unknown, expected: unknown, message?: string): void
    match(value: string, re: RegExp, message?: string): void
    doesNotMatch(value: string, re: RegExp, message?: string): void
    fail(message?: string): never
    throws(fn: () => unknown, message?: string): void
  }
  const assert: Assert
  export default assert
}

declare module "node:child_process" {
  export function spawnSync(
    command: string,
    args: readonly string[],
    options: {
      input?: string
      encoding: "utf8"
      cwd?: string
      env?: Record<string, string | undefined>
      timeout?: number
    },
  ): {
    status: number | null
    signal: string | null
    stdout: string
    stderr: string
    error?: { message: string }
  }
}

declare const process: {
  readonly env: Record<string, string | undefined>
  readonly argv: readonly string[]
  readonly execPath: string
  readonly platform: string
  readonly stdin: {
    setEncoding(encoding: "utf8"): void
    on(event: "data", cb: (chunk: string) => void): void
    on(event: "end" | "error", cb: () => void): void
    resume(): void
  }
  readonly stdout: { write(text: string): void }
  readonly stderr: { write(text: string): void }
  exitCode: number
  exit(code?: number): never
}

declare const console: {
  error(...args: unknown[]): void
  log(...args: unknown[]): void
}

interface ImportMeta {
  readonly url: string
}
