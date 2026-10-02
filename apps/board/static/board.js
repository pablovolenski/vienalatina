// The only JavaScript in the members area: "are you sure?" before a form that
// destroys something.
//
// It lives in a file rather than in onsubmit="" attributes because the
// Content-Security-Policy set in app.py forbids inline script. An inline
// handler under that policy is not an error anyone sees — the browser simply
// ignores it, the dialog never appears, and the delete button quietly loses its
// guard. One listener on the document covers every form, including ones added
// to templates later.
document.addEventListener("submit", function (event) {
  var message = event.target.getAttribute("data-confirm");
  if (message && !window.confirm(message)) {
    event.preventDefault();
  }
});

// --- the writing boxes ----------------------------------------------------
//
// A toolbar, a preview and thumbnails, all of it ours. Not because a library
// would be wrong, but because the Content-Security-Policy here loads scripts
// from this origin only, and vendoring an editor means owning its updates
// forever — for buttons that wrap a selection in asterisks.
//
// Everything below attaches on load and leaves anything it does not recognise
// alone, so a form that has not opted in behaves exactly as it did.

(function () {
  "use strict";

  // What each button does to the selected text. `wrap` goes on both sides,
  // `prefix` goes at the start of every selected line.
  var TOOLS = [
    { label: "N", title: "Negrita", wrap: "**" },
    { label: "I", title: "Cursiva", wrap: "_" },
    { label: "H", title: "Título", prefix: "## " },
    { label: "“”", title: "Cita", prefix: "> " },
    { label: "•", title: "Lista", prefix: "- " },
    { label: "🔗", title: "Enlace", link: true }
  ];

  function apply(box, tool) {
    var start = box.selectionStart;
    var end = box.selectionEnd;
    var selected = box.value.slice(start, end);
    var replacement;

    if (tool.link) {
      var url = window.prompt("Dirección del enlace", "https://");
      if (!url) return;
      replacement = "[" + (selected || "texto") + "](" + url + ")";
    } else if (tool.prefix) {
      // Line by line, so marking three lines as a list marks three lines.
      replacement = (selected || "").split("\n").map(function (line) {
        return line.indexOf(tool.prefix) === 0 ? line : tool.prefix + line;
      }).join("\n");
    } else {
      replacement = tool.wrap + (selected || "texto") + tool.wrap;
    }

    box.setRangeText(replacement, start, end, "end");
    box.focus();
    // Fires the input event frameworks and our own counters would expect; the
    // browser does not fire one for a programmatic change.
    box.dispatchEvent(new Event("input", { bubbles: true }));
  }

  function toolbarFor(box) {
    var bar = document.createElement("div");
    bar.className = "md-bar";
    TOOLS.forEach(function (tool) {
      var button = document.createElement("button");
      button.type = "button";          // never a submit inside a form
      button.className = "md-bar__button";
      button.title = tool.title;
      button.textContent = tool.label;
      button.addEventListener("click", function () { apply(box, tool); });
      bar.appendChild(button);
    });

    var preview = document.createElement("button");
    preview.type = "button";
    preview.className = "md-bar__button md-bar__button--wide";
    preview.textContent = "Vista previa";
    var panel = document.createElement("div");
    panel.className = "md-preview";
    panel.hidden = true;

    preview.addEventListener("click", function () {
      if (!panel.hidden) {
        panel.hidden = true;
        return;
      }
      var token = box.form && box.form.querySelector("input[name=csrf_token]");
      var data = new FormData();
      data.append("body", box.value);
      data.append("csrf_token", token ? token.value : "");
      panel.textContent = "Un momento…";
      panel.hidden = false;
      fetch("/comunidad/previsualizar", {
        method: "POST", body: data, credentials: "same-origin"
      }).then(function (response) {
        return response.ok ? response.json() : null;
      }).then(function (result) {
        // The server rendered this with the same markdown the site uses, with
        // raw HTML disabled, so what comes back is already safe to insert —
        // and inserting it as text would show the tags instead of the writing.
        panel.innerHTML = result ? result.html : "";
        if (!result) panel.textContent = "No se pudo generar la vista previa.";
      }).catch(function () {
        panel.textContent = "No se pudo generar la vista previa.";
      });
    });

    bar.appendChild(preview);
    box.parentNode.insertBefore(bar, box);
    box.parentNode.insertBefore(panel, box.nextSibling);
  }

  // --- thumbnails of what was just chosen ---------------------------------
  //
  // So that attaching the wrong photograph is noticed before posting rather
  // than after. createObjectURL gives a handle to bytes already in this page —
  // nothing is uploaded until the form is submitted — which is what `blob:` in
  // the image policy is there for.
  function previewFiles(input) {
    var strip = input.nextElementSibling;
    if (!strip || !strip.classList.contains("file-strip")) {
      strip = document.createElement("div");
      strip.className = "file-strip";
      input.parentNode.insertBefore(strip, input.nextSibling);
    }
    Array.prototype.forEach.call(strip.querySelectorAll("img"), function (img) {
      URL.revokeObjectURL(img.src);          // or every re-pick leaks one
    });
    strip.textContent = "";

    Array.prototype.forEach.call(input.files, function (file) {
      if (file.type.indexOf("image/") !== 0) return;
      var figure = document.createElement("figure");
      figure.className = "file-strip__item";
      var img = document.createElement("img");
      img.src = URL.createObjectURL(file);
      img.alt = "";
      var caption = document.createElement("figcaption");
      caption.textContent = file.name + " · " + Math.round(file.size / 1024) + " KB";
      figure.appendChild(img);
      figure.appendChild(caption);
      strip.appendChild(figure);
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    Array.prototype.forEach.call(
      document.querySelectorAll("textarea[data-markdown]"), toolbarFor);

    document.addEventListener("change", function (event) {
      var input = event.target;
      if (input.tagName === "INPUT" && input.type === "file" &&
          (input.accept || "").indexOf("image") !== -1) {
        previewFiles(input);
      }
    });
  });
})();
