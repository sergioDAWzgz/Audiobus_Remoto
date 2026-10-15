import { defineConfig } from "vitest/config";
import { cloudflareTest } from "@cloudflare/vitest-plugin";

// e2e tests run the real Worker + SessionDO inside workerd (via Miniflare), so the
// relay/consent behavior under test is exactly what ships — not a mock of it. The
// cloudflareTest plugin wires the Cloudflare pool from the project's wrangler config.
// (.mts so the config is loaded as ESM — @cloudflare/vitest-plugin is ESM-only.)
export default defineConfig({
  plugins: [cloudflareTest({ wrangler: { configPath: "./wrangler.jsonc" } })],
  test: {
    include: ["test/**/*.test.ts"],
  },
});
