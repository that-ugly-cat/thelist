/* Dragging, the description expander, the task modal, the large editor.
 * No framework: the whole app is one list.
 */
(function () {
  const list = document.getElementById("tasks");
  const msg = document.getElementById("ordermsg");
  if (!list) return;

  const sortable = list.dataset.sortable === "1";
  let dragged = null;

  // ── dragging, but only from the handle ──
  //
  // The row is not draggable in the markup; the grip turns it on for the length
  // of one drag and turns it off again. A permanently draggable row is why the
  // description expander looked broken: on a draggable element the mousedown
  // starts a drag gesture, and browsers disagree about whether the click that
  // follows still fires. It also makes the text inside unselectable, which on a
  // row that now shows two lines of description is worse.
  if (sortable) {
    list.querySelectorAll(".task").forEach((li) => {
      const grip = li.querySelector(".grip");
      if (grip) {
        grip.addEventListener("pointerdown", () => { li.draggable = true; });
        grip.addEventListener("pointerup", () => { li.draggable = false; });
      }

      li.addEventListener("dragstart", (e) => {
        if (!li.draggable) return;
        dragged = li;
        li.classList.add("dragging");
        e.dataTransfer.effectAllowed = "move";
        // Firefox will not start a drag without data on the transfer.
        e.dataTransfer.setData("text/plain", li.dataset.id);
      });

      li.addEventListener("dragend", () => {
        li.classList.remove("dragging");
        li.draggable = false;
        list.querySelectorAll(".task").forEach((x) => x.classList.remove("over"));
        dragged = null;
        save();
      });

      li.addEventListener("dragover", (e) => {
        if (!dragged) return;
        e.preventDefault();
        if (dragged === li) return;
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

  // ── the description: two lines, then all of it ──
  // Clamped rather than hidden, so the description is visible without asking —
  // an expander you have to discover is a description nobody reads.
  function expand(btn) {
    const box = btn.closest(".descbox");
    const body = box && box.querySelector(".desc");
    if (!body) return;
    const open = body.classList.toggle("clamped") === false;
    btn.classList.toggle("open", open);
    btn.textContent = open ? "description ▾" : "description";
  }

  // ── one task, in a modal ──
  // Wide, because a task holds notes, links and contacts, and a 380px column
  // turns every one of them into a two-word line.
  function openTask(url) {
    fetch(url, { headers: { "X-Requested-With": "fetch" } })
      .then((r) => r.text())
      .then((html) => {
        const dlg = document.createElement("dialog");
        dlg.className = "taskmodal";
        dlg.innerHTML = html;
        document.body.appendChild(dlg);
        dlg.showModal();
        // A click on the backdrop closes it: the dialog element itself covers
        // only the panel, so a click landing on <dialog> is a click outside.
        dlg.addEventListener("click", (e) => {
          if (e.target === dlg) closeTask(dlg);
        });
      });
  }

  function closeTask(dlg) {
    dlg.close();
    dlg.remove();
    // Reload once, because everything inside the modal is a form that changed
    // something behind it — a stale list under a closed dialog is a lie.
    location.reload();
  }

  // ── the large editor ──
  // A dialog with its own form posting to the same endpoint, and the text
  // carried across in both directions — including on cancel, so a draft is
  // never lost. Sharing one textarea between the two sizes would mean moving a
  // live element in and out of a dialog, which loses focus, selection and,
  // in the failure case that matters, whatever was typed.
  function bigEditor(btn) {
    const small = document.getElementById(btn.dataset.from);
    const dlg = document.createElement("dialog");
    dlg.className = "bigeditor";
    dlg.innerHTML =
      '<form method="post" class="bigform">' +
      "  <h3>Note</h3>" +
      '  <textarea name="body" rows="18" required></textarea>' +
      '  <p class="muted small">Notes cannot be edited afterwards. ' +
      "     Write something you are willing to leave standing.</p>" +
      '  <div class="btnrow">' +
      '    <button class="btn" type="submit">Add note</button>' +
      '    <button class="btn ghost" type="button" data-cancel>Cancel</button>' +
      "  </div>" +
      "</form>";
    const form = dlg.querySelector("form");
    const big = dlg.querySelector("textarea");
    form.action = btn.dataset.bigeditor;
    big.value = small ? small.value : "";
    document.body.appendChild(dlg);
    dlg.showModal();
    big.focus();
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
      expand(expander);
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
      openTask(opener.dataset.panel);
      return;
    }
    const closer = e.target.closest("[data-close]");
    if (closer) {
      e.preventDefault();
      const dlg = closer.closest("dialog");
      if (dlg) closeTask(dlg);
      else location.reload();
    }
  });
})();
