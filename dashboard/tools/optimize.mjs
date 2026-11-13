import fs from "node:fs/promises";
import path from "node:path";
import { transform } from "esbuild";
import { transform as cssTransform } from "lightningcss";
import { minify } from "html-minifier-terser";

const root = process.argv[2] || "/client";

const excluded = new Set([
  "node_modules",
  ".git",
  "tools"
]);

async function walk(directory) {
  const entries = await fs.readdir(directory, {
    withFileTypes: true
  });

  for (const entry of entries) {
    const file = path.join(directory, entry.name);

    if (entry.isDirectory()) {
      if (!excluded.has(entry.name)) {
        await walk(file);
      }
      continue;
    }

    if (!entry.isFile()) continue;

    const ext = path.extname(file).toLowerCase();

    try {
      if ([".js", ".mjs"].includes(ext)) {
        const source = await fs.readFile(file, "utf8");

        const result = await transform(source, {
          loader: "js",
          target: "es2020",
          minify: true,
          legalComments: "none"
        });

        await fs.writeFile(file, result.code);

      } else if (ext === ".css") {
        const source = await fs.readFile(file);

        const result = cssTransform({
          filename: file,
          code: source,
          minify: true
        });

        await fs.writeFile(file, result.code);

      } else if ([".html", ".htm"].includes(ext)) {
        const source = await fs.readFile(file, "utf8");

        const result = await minify(source, {
          collapseWhitespace: true,
          removeComments: true,
          removeRedundantAttributes: true,
          removeEmptyAttributes: true,
          minifyCSS: true,
          minifyJS: true
        });

        await fs.writeFile(file, result);
      }

    } catch (error) {
      console.error(`Optimization failed: ${file}`);
      throw error;
    }
  }
}

await walk(root);

console.log("Production optimization completed.");