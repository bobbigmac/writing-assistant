#!/usr/bin/env node
/**
 * postinstall script — copies the writing-assistant skill to the global
 * Devin skills directory so it's available in every project/session.
 *
 * Target: ~/.config/devin/skills/writing-assistant/SKILL.md
 *
 * This runs automatically on `npm install -g writing-assistant`.
 * It can also be run manually: `node scripts/install-skill.mjs`
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const skillSrc = path.resolve(__dirname, "..", "skills", "writing-assistant", "SKILL.md");
const skillDestDir = path.join(process.env.HOME || "", ".config", "devin", "skills", "writing-assistant");
const skillDest = path.join(skillDestDir, "SKILL.md");

try {
  fs.mkdirSync(skillDestDir, { recursive: true });
  fs.copyFileSync(skillSrc, skillDest);
  console.log(`[writing-assistant] skill installed to ${skillDest}`);
} catch (e) {
  console.warn(`[writing-assistant] could not install skill: ${e.message}`);
  console.warn(`[writing-assistant] the service works without it, but Devin won't know the watch-loop pattern.`);
  console.warn(`[writing-assistant] copy skills/writing-assistant/SKILL.md to ${skillDest} manually.`);
}
