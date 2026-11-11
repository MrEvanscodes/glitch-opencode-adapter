// Answers a context carrying what it received: stdin, argv mode, and the bytecode env flag.
let s = ""; process.stdin.on("data", (c) => (s += c)); process.stdin.on("end", () => {
  process.stdout.write(JSON.stringify({ context: JSON.stringify({ stdin: s, mode: process.argv[2], pydwb: process.env.PYTHONDONTWRITEBYTECODE ?? null }) }));
});
