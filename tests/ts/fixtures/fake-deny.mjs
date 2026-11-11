process.stdin.resume(); process.stdin.on("end", () => {
  process.stdout.write(JSON.stringify({ deny: { kind: "git-clean", member: "Blocked for you.", model: "BLOCKED: forced git clean." } }));
});
