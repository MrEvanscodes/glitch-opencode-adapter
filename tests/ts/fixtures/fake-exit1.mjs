// Reads stdin, echoes it to stderr (so a leak would show), exits 1.
let s = ""; process.stdin.on("data", (c) => (s += c)); process.stdin.on("end", () => { process.stderr.write(s); process.stdout.write(s); process.exit(1); });
