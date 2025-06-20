document.addEventListener("DOMContentLoaded", async () => {
  const repo = "I-am-PUID-0/DUMB";

  try {
    const [repoRes, releaseRes] = await Promise.all([
      fetch(`https://api.github.com/repos/${repo}`),
      fetch(`https://api.github.com/repos/${repo}/releases/latest`)
    ]);

    const repoData = await repoRes.json();
    const releaseData = await releaseRes.json();

    const facts = document.querySelectorAll(".md-header__source .md-source__fact");

    facts.forEach((el) => {
      const text = el.textContent.trim();

      if (/^\d+\.\d+\.\d+$/.test(text)) {
        el.textContent = releaseData.tag_name || "–";
      }

      else if (text === "183" || text === repoData.stargazers_count?.toString()) {
        el.textContent = repoData.stargazers_count ?? "–";
      }

      else if (text === "12" || text === repoData.forks_count?.toString()) {
        el.textContent = repoData.forks_count ?? "–";
      }
    });

    console.log("✅ GitHub metadata updated dynamically");

  } catch (err) {
    console.warn("❌ GitHub metadata update failed:", err);
  }
});
