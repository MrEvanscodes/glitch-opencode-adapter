// index.ts - the Glitch plugin for OpenCode v2.2.7. All logic lives in setup.ts (testable without
// the host); this file only hands it to the host. `@opencode/plugin` is resolved by OpenCode itself
// (measured, docs/api-findings.md 1c): no package.json, no node_modules, no build step.
import { Plugin } from "@opencode/plugin"
import { setupGlitch } from "./setup.ts"

export default Plugin.define({
  id: "glitch",
  setup: (ctx: any) => setupGlitch(ctx),
})
