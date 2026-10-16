/**
 * Tester r1: the browser's zip reader against REAL zips made by Python's zipfile
 * (tests/_support/v08_g4_zip_fixtures.py -> zips_v08_g4_tester_r1/), plus hostile ones.
 */
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { crc32 as nodeCrc32 } from "node:zlib";
import { randomBytes } from "node:crypto";

import { describe, expect, it, vi } from "vitest";

import { ProjectError, walkProject } from "./projectWalk";
import { readZip, crc32 } from "./zip";

const DIR = join(process.cwd(), "src/pages/jobs/submit/zips_v08_g4_tester_r1");
const zip = (name: string) => new File([readFileSync(join(DIR, name))], name);

describe("zip reader vs real zips", () => {
  it("crc32 equals node's zlib.crc32 on random data", () => {
    for (const n of [0, 1, 7, 255, 4096, 100_000]) {
      const data = new Uint8Array(randomBytes(n));
      expect(crc32(data)).toBe(nodeCrc32(data));
    }
  });

  it("normal.zip: unwraps the top folder, unicode name, exec bit, empty dir, zero-byte file", async () => {
    const source = await readZip(zip("normal.zip"));
    expect(source.name).toBe("proj");
    expect(source.dirs.sort()).toEqual(["data", "empty"].filter((d) => source.dirs.includes(d)).sort());
    const byPath = new Map(source.files.map((f) => [f.path, f]));
    expect(byPath.get("run.sh")?.executable).toBe(true);
    expect(byPath.get("main.py")?.executable).toBe(false);
    expect(byPath.get("zero.txt")?.blob.size).toBe(0);
    expect(await byPath.get("data/日本語 😀.txt")?.blob.text()).toBe("é");
    expect(source.dirs).toContain("empty");
    const walk = await walkProject(source);
    expect(walk.entries.some((e) => e.kind === "dir" && e.path === "empty")).toBe(true);
  });

  it("many.zip: 601 deflated entries all read, contents intact", async () => {
    const source = await readZip(zip("many.zip"));
    expect(source.files).toHaveLength(601);
    const f77 = source.files.find((f) => f.path === "d17/f77.txt");
    expect(await f77?.blob.text()).toBe("77");
  });

  for (const name of readdirSync(DIR).filter((n) => n.startsWith("bad_"))) {
    it(`${name}: a path that escapes / is not portable never becomes part of the project`, async () => {
      let outcome: "refused" | "paths" = "refused";
      let paths: string[] = [];
      try {
        const source = await readZip(zip(name));
        const walk = await walkProject(source);
        outcome = "paths";
        paths = walk.entries.map((e) => e.path);
      } catch (error) {
        expect(error).toBeInstanceOf(ProjectError);
      }
      if (outcome === "paths") {
        for (const p of paths) {
          expect(p.startsWith("/") || p.split("/").includes("..") || p.includes("\\") || /^[A-Za-z]:/.test(p), p).toBe(false);
        }
      }
    });
  }

  it("duplicate names are refused, not silently last-wins", async () => {
    await expect(readZip(zip("dupe.zip")).then(walkProject)).rejects.toBeInstanceOf(ProjectError);
  });

  it("symlink entries are refused with the reason", async () => {
    await expect(readZip(zip("symlink.zip"))).rejects.toThrow(/symlink/);
  });

  it("empty / only-directories zips do not crash the reader (the manifest step then says no entrypoint)", async () => {
    const empty = await readZip(zip("empty.zip"));
    expect(empty.files).toEqual([]);
    const dirs = await readZip(zip("onlydirs.zip"));
    expect(dirs.files).toEqual([]);
  });

  it("garbage, truncated and zero-length 'zips' are a ProjectError, never a crash or hang", async () => {
    for (const name of ["garbage.zip", "truncated.zip"]) {
      await expect(readZip(zip(name)), name).rejects.toBeInstanceOf(ProjectError);
    }
    await expect(readZip(new File([], "z.zip"))).rejects.toBeInstanceOf(ProjectError);
  });

  it("T-11 a Finder zip (__MACOSX/ resource forks beside the project folder) still unwraps the project", async () => {
    const source = await readZip(zip("finder.zip"));
    expect(source.files.map((f) => f.path)).toContain("main.py");
  });

  it("fix r1: a Finder zip unwraps to the project folder's name and carries no __MACOSX entry", async () => {
    const source = await readZip(zip("finder.zip"));
    expect(source.name).toBe("proj");
    expect(source.files.map((f) => f.path)).toEqual(["main.py"]);
    expect([...source.files.map((f) => f.path), ...source.dirs].some((p) => p.includes("__MACOSX"))).toBe(false);
  });

  it("T-12 a zip whose central directory understates a size is refused before materialising the real size", async () => {
    // bomb.zip: 64 MiB of zeros declared as 10 bytes. The reader must not inflate it all into memory first.
    const biggest: number[] = [];
    // eslint-disable-next-line @typescript-eslint/unbound-method
    const original: (this: Response) => Promise<ArrayBuffer> = Response.prototype.arrayBuffer;
    vi.spyOn(Response.prototype, "arrayBuffer").mockImplementation(async function (this: Response) {
      const buf = await original.call(this);
      biggest.push(buf.byteLength);
      return buf;
    });
    await expect(readZip(zip("bomb.zip"))).rejects.toBeInstanceOf(ProjectError);
    expect(Math.max(0, ...biggest)).toBeLessThan(1024 * 1024);
  });
});
