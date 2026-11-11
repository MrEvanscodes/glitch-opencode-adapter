process.stdin.resume(); process.stdin.on("end", () => { process.stdout.write(JSON.stringify({ context: "You are Glitch." })); });
