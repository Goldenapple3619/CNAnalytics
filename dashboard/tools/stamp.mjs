#!/usr/bin/env node
/**
 * Stamp a build version on the client so the browser can cache it for a year.
 *
 *   node tools/stamp.mjs [clientRoot]        (default: the folder above this file)
 *
 * What it does:
 *   1. hashes everything that is code (lib/, script/, style/, connect/, panel/, maintenance/) into a short version;
 *   2. writes `?v=<version>` on the entry pages' own references (scripts, boot.css, index.json).
 *
 * From there the loader (lib/lrender.js) reads the version from the manifest URL and requests every file the
 * manifest lists as `url?v=<version>`. nginx serves any request that carries a hex `v` as immutable
 * (see nginx/default.conf), while the HTML pages and anything without `v` are revalidated on every visit.
 * A new deploy changes the hash, so every URL changes with it: nobody is ever served stale code.
 *
 * It is idempotent: running it twice gives the same version, because old `?v=` values are ignored when hashing.
 * The Dockerfile runs it in a build stage; run it by hand only on a copy, since it edits the HTML in place.
 */
import {createHash} from "node:crypto";
import {readdirSync, readFileSync, writeFileSync, statSync, existsSync} from "node:fs";
import {join, relative, resolve, dirname} from "node:path";
import {fileURLToPath} from "node:url";

var root = resolve(process.argv[2] || join(dirname(fileURLToPath(import.meta.url)), ".."));
var HASHED = ["lib", "script", "style", "connect", "panel", "maintenance"];
var ENTRIES = ["panel/index.html", "connect/index.html", "maintenance/index.html"];
var STAMP = /\?v=[0-9a-f]+/g;

function walk(dir) {
    if (!existsSync(dir))
        return ([]);
    return (readdirSync(dir).sort().flatMap((name) => {
        var path = join(dir, name);

        return (statSync(path).isDirectory() ? walk(path) : [path]);
    }));
}

var hash = createHash("sha256");

HASHED.flatMap((dir) => walk(join(root, dir))).forEach((file) => {
    hash.update(relative(root, file).split("\\").join("/") + "\0");
    hash.update(readFileSync(file, "utf8").replace(STAMP, "") + "\0");
});

var version = hash.digest("hex").slice(0, 12);

// Only references to our own code are stamped: /lib, /script, /style, and the manifest.
var REFERENCE = /((?:src|href)=")((?:\/(?:lib|script|style)\/|index\.json)[^"?#]*)(?:\?v=[0-9a-f]+)?(")/g;

ENTRIES.forEach((entry) => {
    var file = join(root, entry);

    if (!existsSync(file))
        return;
    var before = readFileSync(file, "utf8");
    var after = before.replace(REFERENCE, (all, open, url, close) => open + url + "?v=" + version + close);

    if (after !== before)
        writeFileSync(file, after);
    console.log(`${entry}: ${(after.match(STAMP) || []).length} references stamped`);
});

console.log(`version ${version}`);
