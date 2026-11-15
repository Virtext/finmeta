// Overlay profile export/import as a single YAML file -- the same mapping
// shape `overlays.profiles.<name>` has in config.yml (style / format_rules /
// elements). Import validates via POST /api/profiles/{name}/yaml before the
// caller saves it through the usual PUT /api/config round-trip.
import * as YAML from 'js-yaml';
import { checkProfileYaml } from './api';

const SUFFIX = '.finmeta-profile.yml';

export function downloadProfile(name: string, profile: unknown): void {
  const text = YAML.dump(profile ?? {}, { noRefs: true, lineWidth: -1 });
  const url = URL.createObjectURL(new Blob([text], { type: 'application/x-yaml' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = `${name}${SUFFIX}`;
  a.click();
  URL.revokeObjectURL(url);
}

/** Read + validate a profile file. Returns the profile name to use (from the
 *  file name, asking for a new one if it collides with `existing`) and the
 *  parsed doc, or null if the user cancelled the rename prompt. */
export async function readProfileFile(
  file: File, existing: string[],
): Promise<{ name: string; profile: Record<string, unknown> } | null> {
  if (file.size > 1_000_000) throw new Error('profile file is too large (max 1 MB)');
  const text = await file.text();
  let name = file.name.replace(SUFFIX, '').replace(/\.ya?ml$/i, '')
    .trim().toLowerCase().replace(/[^a-z0-9_-]+/g, '-') || 'imported';
  while (existing.includes(name)) {
    const next = window.prompt(`A profile named "${name}" already exists. Import as:`, `${name}-imported`);
    if (!next) return null;
    name = next.trim().toLowerCase().replace(/[^a-z0-9_-]+/g, '-');
  }
  await checkProfileYaml(name, text); // 422 with a readable detail on bad shape/keys
  const profile = (YAML.load(text) as Record<string, unknown> | null) ?? {};
  return { name, profile };
}

/** Open a file picker; resolves with the chosen file (never resolves on cancel). */
export function pickFile(onFile: (f: File) => void): void {
  const input = document.createElement('input');
  input.type = 'file';
  input.accept = '.yml,.yaml';
  input.onchange = () => { const f = input.files?.[0]; if (f) onFile(f); };
  input.click();
}
