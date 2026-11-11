// Prints non-JSON that includes the payload, exits 0.
let s = ""; process.stdin.on("data", (c) => (s += c)); process.stdin.on("end", () => { process.stdout.write("not json at all " + s); });
