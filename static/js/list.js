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

  // ── the description expander ──
  // A button rather than <details>: the row it lives in is draggable, and a
  // <summary> inside a draggable element is where browsers stop agreeing.
  function toggle(btn) {
    const body = document.getElementById(btn.dataset.expand);
    if (!body) return;
    const open = body.classList.toggle("hidden") === false;
    btn.setAttribute("aria-expanded", String(open));
    btn.classList.toggle("open", open);
  }

  // ── the large editor ──
  // A dialog with its own form posting to the same endpoint, and the text
  // carried across on open. Sharing one textarea between the two sizes would
  // mean moving a live element in and out of a dialog, which loses focus,
  // selection and — in the failure case that matters — whatever was typed.
  function bigEditor(btn) {
    const small = document.getElementById(btn.dataset.from);
    const dlg = document.createElement("dialog");
    dlg.className = "bigeditor";
    dlg.innerHTML =
      '<form method="post" class="bigform">' +
      '  <h3>Note</h3>' +
      '  <textarea name="body" rows="18" required></textarea>' +
      '  <p class="muted small">Notes cannot be edited afterwards. ' +
      '     Write something you are willing to leave standing.</p>' +
      '  <div class="btnrow">' +
      '    <button class="btn" type="submit">Add note</button>' +
      '    <button class="btn ghost" type="button" data-cancel>Cancel</button>' +
      '  </div>' +
      "</form>";
    const form = dlg.querySelector("form");
    const big = dlg.querySelector("textarea");
    form.action = btn.dataset.bigeditor;
    big.value = small ? small.value : "";
    document.body.appendChild(dlg);
    dlg.showModal();
    big.focus();
    // Carry the text back on the way out, so a cancel does not lose the draft.
    const close = () => {
      if (small) small.value = big.value;
      dlg.close();
      dlg.remove();
    };
    dlg.querySelector("[data-cancel]").addEventListener("click", close);
    dlg.addEventListener("cancel", (ev) => { ev.preventDefault(); close(); });
  }

  document.addEventListener("click", (e) => {
    const expander = e.target.closest("[data-expand]");
    if (expander) {
      e.preventDefault();
      toggle(expander);
      return;
    }
    const big = e.target.closest("[data-bigeditor]");
    if (big) {
      e.preventDefault();
      bigEditor(big);
      return;
    }
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
