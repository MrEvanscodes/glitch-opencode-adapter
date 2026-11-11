// An {error} label that is far too long and has a newline in it.
process.stdin.resume(); process.stdin.on("end", () => { process.stdout.write(JSON.stringify({ error: "x".repeat(5000) + "\nsecond line" })); });
