// seven.mjs secret's policy files (2026-11-21, README "Logins on seven"), as a module of its own so the tests can
// import it. spec: { NAME: { placeholder, domains, header, methods, paths } }, already checked by seven.mjs.
const tomlList = (xs) => `[${xs.map((x) => JSON.stringify(x)).join(', ')}]`;

export function secretFiles(spec) {
  const names = Object.keys(spec).sort();
  let fnox = 'default_provider = "seven"\n\n[providers.seven]\ntype = "sealed"\n\n[secrets]\n';
  for (const n of names) fnox += `${n} = { provider = "seven", value = "${spec[n].placeholder}" }\n`;
  for (const n of names) {
    for (const d of spec[n].domains) {
      fnox += `\n[[proxy.rules]]\nsecret = "${n}"\ndomain = "${d}"\nheader = "${spec[n].header}"\n`;
      if (spec[n].methods.length) fnox += `methods = ${tomlList(spec[n].methods)}\n`;
      if (spec[n].paths.length) fnox += `paths = ${tomlList(spec[n].paths)}\n`;
    }
  }
  let policy = '[exits]\n';
  for (const n of names) policy += `${n} = ["page"]\n`;
  policy += '\n[reveal]\nagent = false\n';
  let profile = '# mcp-slicc (seven.mjs secret): this job\'s credentials, as seven\'s vault placeholders. The vault puts the\n'
    + '# value in only on the rule\'s hosts, on the way out.\n';
  for (const n of names) profile += `export ${n}=${spec[n].placeholder}\n`;
  return { fnox, policy, profile };
}
