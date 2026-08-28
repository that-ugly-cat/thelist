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

  // ── the description: 180 characters, then all of it ──
  // Cut on characters and not on rendered lines, because a line is however much
  // fits in the window: the same task read as three words on a phone and as a
  // paragraph on a desktop. Both halves are in the DOM and the button swaps
  // them, so opening costs no request.
  function expand(btn) {
    const box = btn.closest(".descbox");
    if (!box) return;
    const short = box.querySelector(".short");
    const full = box.querySelector(".full");
    if (!short || !full) return;
    const opening = full.classList.contains("hidden");
    full.classList.toggle("hidden", !opening);
    short.classList.toggle("hidden", opening);
    btn.classList.toggle("open", opening);
  }

  // ── tags as pills, WordPress-style ──
  //
  // A comma turns what precedes it into a pill; backspace on an empty box takes
  // the last one back. The hidden field carries the comma-joined value, so the
  // server keeps receiving exactly the string it received before and nothing
  // downstream had to change — the widget is a way of typing, not a new format.
  //
  // Whatever is still in the box when the form is submitted counts as a tag:
  // typing a name and pressing Add should not silently drop it because no comma
  // was typed after it.
  function initTags(box) {
    const entry = box.querySelector(".tagentry");
    const hidden = box.querySelector('input[type="hidden"]');
    let tags = (hidden.value || "").split(",").map((t) => t.trim()).filter(Boolean);

    function sync() {
      hidden.value = tags.join(", ");
      box.querySelectorAll(".tagpill").forEach((p) => p.remove());
      tags.forEach((t, i) => {
        const pill = document.createElement("span");
        pill.className = "pill tag tagpill";
        pill.textContent = t;
        const x = document.createElement("button");
        x.type = "button";
        x.className = "tagx";
        x.textContent = "×";
        x.addEventListener("click", () => { tags.splice(i, 1); sync(); });
        pill.appendChild(x);
        box.insertBefore(pill, entry);
      });
    }

    function commit(raw) {
      raw.split(",").map((t) => t.trim()).filter(Boolean).forEach((t) => {
        if (!tags.some((x) => x.toLowerCase() === t.toLowerCase())) tags.push(t);
      });
      entry.value = "";
      sync();
    }

    entry.addEventListener("keydown", (e) => {
      if (e.key === "," || e.key === "Enter") {
        // Enter must not submit the form from inside the tag box: people press
        // it expecting to end a tag, not to save half a task.
        e.preventDefault();
        commit(entry.value);
      } else if (e.key === "Backspace" && !entry.value && tags.length) {
        tags.pop();
        sync();
      }
    });
    // A datalist pick fires input, not keydown, and arrives with no comma.
    entry.addEventListener("change", () => { if (entry.value) commit(entry.value); });
    entry.addEventListener("blur", () => { if (entry.value) commit(entry.value); });
    const form = box.closest("form");
    if (form) form.addEventListener("submit", () => { if (entry.value) commit(entry.value); });
    sync();
  }

  document.querySelectorAll("[data-taginput]").forEach(initTags);

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
        // The panel arrives after start-up, so anything that had to be wired at
        // load time has to be wired again here. Today that is the tag widget;
        // the rule is that this line goes with any widget added to the panel.
        dlg.querySelectorAll("[data-taginput]").forEach(initTags);
        wireModalForms(dlg, url);
        dlg.showModal();
        // A click on the backdrop closes it: the dialog element itself covers
        // only the panel, so a click landing on <dialog> is a click outside.
        dlg.addEventListener("click", (e) => {
          if (e.target === dlg) closeTask(dlg);
        });
      });
  }

  function closeTask(dlg) {
    const touched = dlg.dataset.touched === "1";
    dlg.close();
    dlg.remove();
    // Reload only if something actually changed while it was open: a stale list
    // under a closed dialog is a lie, but reloading after a pure read throws
    // away the scroll position for nothing.
    if (touched) location.reload();
  }

  // ── forms inside the task modal submit without closing it ──
  //
  // Every one of them used to POST normally, which meant a redirect, a full page
  // load, and the modal gone — so adding three notes meant reopening the task
  // three times. Now the submit goes out by fetch and the panel is re-rendered
  // in place from the same URL that drew it.
  //
  // The list underneath is left stale on purpose and refreshed on close: it is
  // behind a modal nobody can see through, and reloading it under the dialog
  // would be work done for a view that is covered.
  function wireModalForms(dlg, url) {
    dlg.addEventListener("submit", (e) => {
      const form = e.target;
      if (!(form instanceof HTMLFormElement)) return;
      e.preventDefault();
      const body = new URLSearchParams(new FormData(form));
      fetch(form.action, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body,
        redirect: "follow",
      })
        .then((r) => {
          if (!r.ok) return r.text().then((t) => Promise.reject(t));
          dlg.dataset.touched = "1";
          return fetch(url, { headers: { "X-Requested-With": "fetch" } });
        })
        .then((r) => r.text())
        .then((html) => {
          dlg.innerHTML = html;
          dlg.querySelectorAll("[data-taginput]").forEach(initTags);
        })
        .catch((detail) => {
          // The server refuses for reasons a person can act on — an empty note,
          // a link with no address, a reason missing on a decline. Say it inside
          // the modal instead of leaving the click looking ignored.
          const msg = dlg.querySelector(".formerr") || document.createElement("p");
          msg.className = "err formerr";
          msg.textContent = String(detail).replace(/<[^>]*>/g, "").slice(0, 300)
                            || "That did not go through.";
          const head = dlg.querySelector(".panelhead");
          if (head && !msg.parentNode) head.after(msg);
        });
    });
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
    const mark = e.target.closest("[data-earmark]");
    if (mark) {
      e.preventDefault();
      fetch(mark.dataset.earmark, { method: "POST" })
        .then((r) => r.json())
        .then((d) => {
          mark.classList.toggle("on", d.on);
          mark.setAttribute("aria-pressed", String(d.on));
        });
      return;
    }
    const opener2 = e.target.closest("[data-open]");
    if (opener2) {
      e.preventDefault();
      const dlg = document.getElementById(opener2.dataset.open);
      if (dlg) {
        dlg.showModal();
        dlg.addEventListener("click", (ev) => { if (ev.target === dlg) dlg.close(); },
                             { once: true });
      }
      return;
    }
    const dismiss = e.target.closest("[data-dismiss]");
    if (dismiss) {
      e.preventDefault();
      const dlg = dismiss.closest("dialog");
      if (dlg) dlg.close();
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
