/* Dragging, and the side panel. No framework: the whole app is one list.
 *
 * The order the server receives is the COMPLETE array of ids plus the
 * order_version the page was rendered with. With a few dozen rows that costs
 * nothing and has none of the drift of fractional positions — and the version
 * settles the race that genuinely exists when the point of the tool is two
 * people looking at the same list. A refused drag reloads; two overlapping
 * drags applied in an arbitrary order would be worse than losing one.
 */
(function () {
  const list = document.getElementById("tasks");
  const panel = document.getElementById("panel");
  const msg = document.getElementById("ordermsg");
  if (!list) return;

  const sortable = list.dataset.sortable === "1";
  let dragged = null;

  if (sortable) {
    list.querySelectorAll(".task").forEach((li) => {
      li.setAttribute("draggable", "true");

      li.addEventListener("dragstart", (e) => {
        dragged = li;
        li.classList.add("dragging");
        e.dataTransfer.effectAllowed = "move";
        // Firefox will not start a drag without data on the transfer.
        e.dataTransfer.setData("text/plain", li.dataset.id);
      });

      li.addEventListener("dragend", () => {
        li.classList.remove("dragging");
        list.querySelectorAll(".task").forEach((x) => x.classList.remove("over"));
        dragged = null;
        save();
      });

      li.addEventListener("dragover", (e) => {
        e.preventDefault();
        if (!dragged || dragged === li) return;
        li.classList.add("over");
        const box = li.getBoundingClientRect();
        const below = e.clientY > box.top + box.height / 2;
        list.insertBefore(dragged, below ? li.nextSibling : li);
      });

      li.addEventListener("dragleave", () => li.classList.remove("over"));
    });
  }

  function save() {
    const ids = [...list.querySelectorAll(".task")].map((li) => Number(li.dataset.id));
    fetch(`/app/${list.dataset.ws}/order`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids, version: Number(list.dataset.version) }),
    })
      .then((r) => (r.ok ? r.json() : Promise.reject(r)))
      .then((data) => {
        list.dataset.version = data.version;
        if (msg) msg.classList.add("hidden");
      })
      .catch(() => {
        // Someone else moved something, or the list changed underneath. Say so
        // rather than pretending the drag landed.
        if (msg) msg.classList.remove("hidden");
      });
  }

  // ── the side panel ──
  function open(url) {
    fetch(url, { headers: { "X-Requested-With": "fetch" } })
      .then((r) => r.text())
      .then((html) => {
        panel.innerHTML = html;
        panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
      });
  }

  document.addEventListener("click", (e) => {
    const opener = e.target.closest("[data-panel]");
    if (opener) {
      e.preventDefault();
      open(opener.dataset.panel);
      return;
    }
    if (e.target.closest("[data-close]")) {
      e.preventDefault();
      location.reload();
    }
  });
})();
