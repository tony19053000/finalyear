import { deflateRawSync } from "node:zlib";

import { describe, expect, it } from "vitest";

import { IgnoreSpec, compilePattern, rebase, splitLines } from "./gitignore";
import { FileChangedError, hashBlob } from "./hashFile";
import { inlineHasher } from "./hasher";
import { readProjectManifest, sanitizeName } from "./projectManifest";
import { ProjectError, checkRel, walkProject } from "./projectWalk";
import { Sha256, sha256Hex } from "./sha256";
import { canonicalBytes, compareUtf8 } from "./treeManifest";
import { crc32, readZip } from "./zip";
import { folderSource } from "./folderSource";

const enc = new TextEncoder();

describe("Sha256 (streaming)", () => {
  it.each([
    ["", "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"],
    ["abc", "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"],
    [
      "abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq",
      "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1",
    ],
  ])("known vector %#", (text, hex) => {
    expect(new Sha256().update(enc.encode(text)).hex()).toBe(hex);
  });

  it("equals WebCrypto for any split of the input", async () => {
    const data = new Uint8Array(200_003);
    for (let i = 0; i < data.length; i += 1) data[i] = (i * 31 + 7) & 0xff;
    const expected = await sha256Hex(data);
    for (const step of [1, 63, 64, 65, 4096, 77_777]) {
      const h = new Sha256();
      for (let at = 0; at < data.length; at += step) h.update(data.subarray(at, at + step));
      expect(h.hex()).toBe(expected);
    }
  });

  it("refuses use after the digest", () => {
    const h = new Sha256();
    h.hex();
    expect(() => h.update(new Uint8Array(1))).toThrow();
  });
});

describe("hashBlob (tree digests, D80/D81)", () => {
  it("one-chunk file: whole digest, no chunk list", async () => {
    const r = await hashBlob(new Blob(["hello"]), 65_536);
    expect(r).toEqual({ sha256: await sha256Hex(enc.encode("hello")), chunks: [], size: 5 });
  });

  it("chunked file: per-chunk digests + the whole digest, progress reported", async () => {
    const data = new Uint8Array(65_536 * 2 + 10).fill(7);
    const seen: number[] = [];
    const r = await hashBlob(new Blob([data]), 65_536, (b) => seen.push(b));
    expect(r.chunks).toHaveLength(3);
    expect(r.chunks[0]).toBe(await sha256Hex(data.subarray(0, 65_536)));
    expect(r.chunks[2]).toBe(await sha256Hex(data.subarray(131_072)));
    expect(r.sha256).toBe(await sha256Hex(data));
    expect(seen).toEqual([65_536, 131_072, 131_082]);
  });

  it("cancel stops between chunks", async () => {
    const data = new Uint8Array(65_536 * 3);
    await expect(hashBlob(new Blob([data]), 65_536, undefined, () => true)).rejects.toThrow("cancelled");
  });

  it("FileChangedError is a typed error", () => {
    expect(new FileChangedError("x").name).toBe("FileChangedError");
  });

  it("inline hasher reports deltas", async () => {
    const deltas: number[] = [];
    await inlineHasher().hash(new Blob([new Uint8Array(65_537)]), 65_536, (d) => deltas.push(d));
    expect(deltas).toEqual([65_536, 1]);
  });
});

describe("gitignore matcher (pathspec port)", () => {
  const spec = (...lines: string[]) => IgnoreSpec.fromLines(lines);

  it("comments, blanks, '/' alone and invalid ranges are no rules", () => {
    expect(compilePattern("# x")).toBeNull();
    expect(compilePattern("   ")).toBeNull();
    expect(compilePattern("/")).toBeNull();
    expect(compilePattern("[abc")).toBeNull();
  });

  it("an escape at the end is an error (pathspec raises too)", () => {
    expect(() => compilePattern("foo\\")).toThrow();
  });

  it("unanchored names match at any depth; anchored ones only from the root", () => {
    expect(spec("*.log").matches("a/b/c.log")).toBe(true);
    expect(spec("/top.txt").matches("top.txt")).toBe(true);
    expect(spec("/top.txt").matches("a/top.txt")).toBe(false);
    expect(spec("a/b.txt").matches("x/a/b.txt")).toBe(false);
  });

  it("a folder pattern matches the folder and everything below, not a file of that name", () => {
    expect(spec("build/").matches("build/")).toBe(true);
    expect(spec("build/").matches("x/build/y.o")).toBe(true);
    expect(spec("build/").matches("build")).toBe(false);
  });

  it("last rule wins; negation re-includes", () => {
    expect(spec("*.log", "!keep.log").matches("keep.log")).toBe(false);
    expect(spec("!keep.log", "*.log").matches("keep.log")).toBe(true);
  });

  it("** forms, ? and classes", () => {
    expect(spec("a/**/z").matches("a/b/c/z")).toBe(true);
    expect(spec("a/**/z").matches("a/z")).toBe(true);
    expect(spec("**/deep/*.dat").matches("x/deep/y.dat")).toBe(true);
    expect(spec("?.md").matches("ab.md")).toBe(false);
    expect(spec("[!a]x").matches("bx")).toBe(true);
    expect(spec("[!a]x").matches("ax")).toBe(false);
    expect(spec("\\#lit").matches("#lit")).toBe(true);
    expect(spec("trail\\ ").matches("trail ")).toBe(true);
  });

  it("rebase writes nested-folder rules relative to the root", () => {
    expect(rebase("x.txt", "src/")).toBe("src/**/x.txt");
    expect(rebase("/x.txt", "src/")).toBe("src/x.txt");
    expect(rebase("!a/b", "s[1]/")).toBe("!s\\[1\\]/a/b");
    expect(rebase("# c", "src/")).toBeNull();
  });

  it("splitLines splits like Python str.splitlines", () => {
    expect(splitLines("a\r\nb\rc\nd\u2028e\n")).toEqual(["a", "b", "c", "d", "e"]);
  });
});

describe("project walk refusals (common/walk.py check_rel)", () => {
  it.each([
    ["a\\b.txt", "'\\\\'"],
    ["../x", "'..'"],
    ["/abs", "absolute"],
    ["C:/x", "absolute"],
    [`${"a".repeat(256)}.txt`, "longer than 255"],
  ])("%s is refused", (path, why) => {
    expect(() => {
      checkRel(path);
    }).toThrow(new RegExp(why));
  });

  it("an ignore file that is not UTF-8 is refused like the CLI", async () => {
    const source = { name: "p", dirs: [], files: [{ path: ".gitignore", blob: new Blob([new Uint8Array([0xff, 0xfe])]), executable: false }] };
    await expect(walkProject(source)).rejects.toThrow(/not UTF-8/);
  });

  it("the same path twice is refused", async () => {
    const f = { path: "a.txt", blob: new Blob(["x"]), executable: false };
    await expect(walkProject({ name: "p", dirs: [], files: [f, f] })).rejects.toThrow(ProjectError);
  });
});

describe("tree manifest bytes", () => {
  it("canonical JSON: sorted keys, no spaces, raw UTF-8, escapes like Python", () => {
    const bytes = canonicalBytes(65_536, [
      { kind: "dir", path: "e" },
      { kind: "file", path: 'q"\u00e9\t', sha256: "ab", size: 3, executable: true, chunks: [] },
    ]);
    expect(new TextDecoder().decode(bytes)).toBe(
      '{"chunk_size":65536,"entries":[{"dir":true,"path":"e"},{"exec":true,"path":"q\\"\u00e9\\t","sha256":"ab","size":3}],"v":1}',
    );
  });

  it("orders by UTF-8 bytes (not UTF-16)", () => {
    const paths = ["\uffff", "\u{1F600}", "a/b", "a-b", "Z"];
    expect([...paths].sort(compareUtf8)).toEqual(["Z", "a-b", "a/b", "\uffff", "\u{1F600}"]);
  });
});

describe("compute.yaml reading (common/manifest.py load_manifest)", () => {
  const file = (path: string, text: string) => ({ path, blob: new Blob([text]), executable: false });

  it("no manifest: name from the folder (sanitised), entrypoint main.py then __main__.py", async () => {
    const a = await readProjectManifest({ name: "My Project!", dirs: [], files: [file("main.py", ""), file("__main__.py", "")] });
    expect(a).toEqual({ manifest: { name: "My-Project", entrypoint: ["python", "main.py"] }, fromFile: false });
    const b = await readProjectManifest({ name: "x", dirs: [], files: [file("__main__.py", "")] });
    expect(b.manifest.entrypoint).toEqual(["python", "__main__.py"]);
    await expect(readProjectManifest({ name: "x", dirs: [], files: [file("other.py", "")] })).rejects.toThrow(/cannot be inferred/);
  });

  it("reads compute.yaml; keeps its name and entrypoint", async () => {
    const r = await readProjectManifest({
      name: "folder",
      dirs: [],
      files: [file("compute.yaml", "name: n1\nentrypoint: python a.py\nenv:\n  requirements: req.txt\n"), file("req.txt", "")],
    });
    expect(r).toEqual({
      manifest: { name: "n1", entrypoint: "python a.py", env: { requirements: "req.txt" } },
      fromFile: true,
    });
  });

  it("env.requirements must be a file inside the project", async () => {
    const yaml = (r: string) => file("compute.yaml", `entrypoint: x\nenv:\n  requirements: ${r}\n`);
    await expect(readProjectManifest({ name: "p", dirs: [], files: [yaml("missing.txt")] })).rejects.toThrow(/no such file/);
    await expect(readProjectManifest({ name: "p", dirs: [], files: [yaml("../up.txt")] })).rejects.toThrow(/outside/);
    await expect(readProjectManifest({ name: "p", dirs: [], files: [yaml("/etc/passwd")] })).rejects.toThrow(/outside/);
  });

  it("refuses a non-mapping, oversize or non-UTF-8 manifest; flags YAML the browser cannot read", async () => {
    await expect(readProjectManifest({ name: "p", dirs: [], files: [file("compute.yaml", "- a\n")] })).rejects.toThrow(/mapping/);
    const big = { path: "compute.yaml", blob: new Blob([new Uint8Array(65_537)]), executable: false };
    await expect(readProjectManifest({ name: "p", dirs: [], files: [big] })).rejects.toThrow(/larger than/);
    const bad = { path: "compute.yaml", blob: new Blob([new Uint8Array([0xff])]), executable: false };
    await expect(readProjectManifest({ name: "p", dirs: [], files: [bad] })).rejects.toThrow(/UTF-8/);
    await expect(
      readProjectManifest({ name: "p", dirs: [], files: [file("compute.yaml", "entrypoint: |\n  x\n")] }),
    ).rejects.toMatchObject({ useCli: true });
  });

  it("sanitizeName matches the CLI", () => {
    expect(sanitizeName("My Project!")).toBe("My-Project");
    expect(sanitizeName("..__")).toBe("job");
    expect(sanitizeName("a".repeat(80))).toHaveLength(64);
  });
});

/* ------------------------------------------------------------------ zip */

interface ZipItem {
  name: string;
  data?: Uint8Array;
  deflate?: boolean;
  mode?: number;
}

function makeZip(items: ZipItem[]): Uint8Array {
  const parts: Uint8Array[] = [];
  const central: Uint8Array[] = [];
  let offset = 0;
  for (const item of items) {
    const name = enc.encode(item.name);
    const raw = item.data ?? new Uint8Array();
    const packed = item.deflate === true ? new Uint8Array(deflateRawSync(raw)) : raw;
    const crc = crc32(raw);
    const local = new DataView(new ArrayBuffer(30));
    local.setUint32(0, 0x04034b50, true);
    local.setUint16(8, item.deflate === true ? 8 : 0, true);
    local.setUint32(14, crc, true);
    local.setUint32(18, packed.length, true);
    local.setUint32(22, raw.length, true);
    local.setUint16(26, name.length, true);
    parts.push(new Uint8Array(local.buffer), name, packed);
    const cd = new DataView(new ArrayBuffer(46));
    cd.setUint32(0, 0x02014b50, true);
    cd.setUint16(4, (3 << 8) | 20, true);
    cd.setUint16(8, 0x800, true);
    cd.setUint16(10, item.deflate === true ? 8 : 0, true);
    cd.setUint32(16, crc, true);
    cd.setUint32(20, packed.length, true);
    cd.setUint32(24, raw.length, true);
    cd.setUint16(28, name.length, true);
    cd.setUint32(38, ((item.mode ?? 0o100644) << 16) >>> 0, true);
    cd.setUint32(42, offset, true);
    central.push(new Uint8Array(cd.buffer), name);
    offset += 30 + name.length + packed.length;
  }
  const cdSize = central.reduce((s, p) => s + p.length, 0);
  const end = new DataView(new ArrayBuffer(22));
  end.setUint32(0, 0x06054b50, true);
  end.setUint16(8, items.length, true);
  end.setUint16(10, items.length, true);
  end.setUint32(12, cdSize, true);
  end.setUint32(16, offset, true);
  const all = [...parts, ...central, new Uint8Array(end.buffer)];
  const out = new Uint8Array(all.reduce((s, p) => s + p.length, 0));
  let at = 0;
  for (const p of all) {
    out.set(p, at);
    at += p.length;
  }
  return out;
}

const zipFile = (bytes: Uint8Array, name = "proj.zip") => new File([bytes as Uint8Array<ArrayBuffer>], name);

describe("zip reader", () => {
  it("reads stored + deflated entries, unwraps one top folder, keeps exec bits and empty folders", async () => {
    const zip = makeZip([
      { name: "proj/" },
      { name: "proj/main.py", data: enc.encode("print(1)\n") },
      { name: "proj/data/big.txt", data: enc.encode("x".repeat(5000)), deflate: true },
      { name: "proj/run.sh", data: enc.encode("#!/bin/sh\n"), mode: 0o100755 },
      { name: "proj/empty/" },
    ]);
    const source = await readZip(zipFile(zip));
    expect(source.name).toBe("proj");
    expect(source.dirs).toEqual(["empty"]);
    expect(source.files.map((f) => [f.path, f.executable])).toEqual([
      ["main.py", false],
      ["data/big.txt", false],
      ["run.sh", true],
    ]);
    expect(await source.files[1]?.blob.text()).toBe("x".repeat(5000));
  });

  it("a flat zip keeps its paths and takes the zip's name", async () => {
    const source = await readZip(zipFile(makeZip([{ name: "main.py", data: enc.encode("1") }]), "My Job.zip"));
    expect(source.name).toBe("My Job");
    expect(source.files[0]?.path).toBe("main.py");
  });

  it("refuses a damaged entry, a symlink, and a non-zip", async () => {
    const zip = makeZip([{ name: "a.txt", data: enc.encode("hello") }]);
    const crcAt = zip.length - 22 - (46 + 5) + 16;
    zip[crcAt] = (zip[crcAt] ?? 0) ^ 0xff;
    await expect(readZip(zipFile(zip))).rejects.toThrow(/damaged/);
    await expect(readZip(zipFile(makeZip([{ name: "l", data: enc.encode("t"), mode: 0o120777 }])))).rejects.toThrow(/symlink/);
    await expect(readZip(zipFile(enc.encode("not a zip at all, sorry")))).rejects.toThrow(/not a zip/);
  });

  it("refuses an archive that unpacks over the limit", async () => {
    const zip = makeZip([{ name: "a", data: new Uint8Array(100) }]);
    await expect(readZip(zipFile(zip), 50)).rejects.toThrow(/more than 50 bytes/);
  });
});

describe("folder picker source", () => {
  const picked = (rel: string) => {
    const f = new File(["x"], rel.split("/").pop() ?? "f");
    Object.defineProperty(f, "webkitRelativePath", { value: rel });
    return f;
  };

  it("strips the picked folder's name and uses it as the job name", () => {
    const s = folderSource([picked("proj/main.py"), picked("proj/a/b.txt")]);
    expect(s.name).toBe("proj");
    expect(s.files.map((f) => f.path)).toEqual(["main.py", "a/b.txt"]);
  });

  it("refuses an empty pick and files from different folders", () => {
    expect(() => folderSource([])).toThrow(/empty/);
    expect(() => folderSource([picked("a/x"), picked("b/y")])).toThrow(/one folder/);
  });
});
