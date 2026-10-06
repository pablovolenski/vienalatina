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
// The box shows the formatting as it is typed: bold is bold, a heading is a
// heading. There is no preview pane — a second box beside the first is the
// wrong shape on a phone — and nothing is sent anywhere while anybody writes.
//
// What is stored is still markdown, which is what keeps everything downstream
// as it is: the file committed to git, the pipeline that translates it, Decap,
// and above all `render.to_html` with raw HTML disabled, which is the whole of
// the XSS defence. The server receives markdown and renders it itself; nothing
// the browser sends is ever treated as HTML.
//
// The two directions are deliberately not symmetrical:
//
//   opening something to edit — the *server* renders the stored markdown into
//     a <template> beside the box and the editor adopts it, so there is no
//     markdown parser in the browser and no second opinion about what
//     somebody's text means;
//   saving — the browser walks a closed set of elements back into markdown
//     (`serialize` below). Anything it does not recognise contributes its text
//     and nothing else, which is why a paste from Word brings no markup in.
//
// With JavaScript off, or if any of this throws, the plain <textarea> is still
// there and still the field that submits: nothing here is hidden by CSS.

(function () {
  "use strict";

  // --- markdown out of the document ---------------------------------------

  var BLOCKS = { H1: 1, H2: 1, H3: 1, H4: 1, H5: 1, H6: 1, P: 1, DIV: 1,
                 UL: 1, OL: 1, BLOCKQUOTE: 1, PRE: 1, HR: 1, TABLE: 1 };
  // H1 becomes ## on purpose: the page's heading is the post's title, and a
  // pasted h1 should not compete with it.
  var HEADING = { H1: "## ", H2: "## ", H3: "### ", H4: "#### ",
                  H5: "#### ", H6: "#### " };
  // A line that opens with something markdown reads as a marker is escaped, or
  // «- 20 € por persona» comes back as a bullet the next time it is opened.
  var MARKER = /^(\s*)(#{1,6}|>|[-+*]|\d{1,9}[.)])(\s)/;

  function escapeText(text) {
    return text.replace(/\s+/g, " ").replace(/([\\`*_[\]])/g, "\\$1");
  }

  // Only schemes that cannot execute. markdown-it refuses `javascript:` on its
  // own, so this is the second of two locks rather than the only one.
  function safeHref(href) {
    return /^(https?:|mailto:|\/|#)/i.test(href || "") ? href : "";
  }

  // «** bold **» is not bold in CommonMark, and a selection almost always
  // carries a space at one end.
  function wrapped(marker, inner) {
    var lead = inner.match(/^\s*/)[0];
    var tail = inner.match(/\s*$/)[0];
    var core = inner.slice(lead.length, inner.length - tail.length);
    return core ? lead + marker + core + marker + tail : inner;
  }

  // Elements whose text is not writing. Nothing here can execute — the server
  // renders markdown with raw HTML disabled — but a stylesheet pasted into the
  // box should not come back as four hundred words of prose either.
  var DROP = { SCRIPT: 1, STYLE: 1, TEMPLATE: 1, NOSCRIPT: 1, IFRAME: 1, OBJECT: 1, HEAD: 1 };

  function inlineOne(node) {
    if (node.nodeType === 3) { return escapeText(node.nodeValue); }
    if (node.nodeType !== 1) { return ""; }
    var tag = node.tagName;
    if (DROP[tag]) { return ""; }
    // Two spaces then a newline: the break CommonMark means wherever it is
    // rendered. A bare newline is a break in the members area and a space on
    // the public site, which would make the same post read differently in the
    // two places.
    if (tag === "BR") { return "  \n"; }
    if (tag === "CODE") { return "`" + node.textContent + "`"; }
    if (tag === "IMG") { return escapeText(node.getAttribute("alt") || ""); }

    var inner = inline(node);
    if (!inner.trim()) { return inner; }
    if (tag === "STRONG" || tag === "B") { return wrapped("**", inner); }
    if (tag === "EM" || tag === "I") { return wrapped("_", inner); }
    if (tag === "S" || tag === "DEL" || tag === "STRIKE") { return wrapped("~~", inner); }
    if (tag === "A") {
      var href = safeHref(node.getAttribute("href"));
      return href ? "[" + inner.trim() + "](" + href + ")" : inner;
    }
    return inner;        // span, font, and whatever else a paste left behind
  }

  function inline(node) {
    var out = "";
    Array.prototype.forEach.call(node.childNodes, function (child) {
      out += inlineOne(child);
    });
    return out;
  }

  function paragraph(text, indent) {
    // Trimmed at the two ends only. Stripping trailing spaces line by line —
    // which is the tidy-looking thing to do — would eat the two spaces that
    // *are* the hard break a <br> turns into.
    var body = text.replace(/^\s+|\s+$/g, "");
    if (!body.trim()) { return ""; }
    return body.split("\n").map(function (line) {
      return indent + line.replace(MARKER, function (whole, space, token, after) {
        return /^\d/.test(token)
          ? space + token.slice(0, -1) + "\\" + token.slice(-1) + after
          : space + "\\" + token + after;
      });
    }).join("\n");
  }

  function list(el, indent) {
    var ordered = el.tagName === "OL";
    var number = 1;
    var lines = [];
    Array.prototype.forEach.call(el.children, function (item) {
      if (item.tagName !== "LI") { return; }
      var marker = ordered ? (number++) + ". " : "- ";
      var head = "";
      var nested = [];
      Array.prototype.forEach.call(item.childNodes, function (child) {
        if (child.nodeType === 1 && (child.tagName === "UL" || child.tagName === "OL")) {
          nested.push(list(child, indent + "  "));
        } else {
          head += inlineOne(child);
        }
      });
      head = head.replace(/\s+/g, " ").trim();
      if (head || nested.length) { lines.push(indent + marker + head); }
      nested.forEach(function (block) { if (block) { lines.push(block); } });
    });
    return lines.join("\n");
  }

  function blockFor(el, indent) {
    var tag = el.tagName;
    if (HEADING[tag]) {
      var title = inline(el).replace(/\s+/g, " ").trim();
      return title ? indent + HEADING[tag] + title : "";
    }
    if (tag === "UL" || tag === "OL") { return list(el, indent); }
    if (tag === "BLOCKQUOTE") {
      return serialize(el, indent).split("\n").map(function (line) {
        return line ? "> " + line : ">";
      }).join("\n");
    }
    if (tag === "PRE") { return indent + "```\n" + el.textContent + "\n```"; }
    if (tag === "HR") { return indent + "---"; }
    // A pasted table keeps its words and loses its grid. Writing one is what
    // the markdown view is for; silently dropping the text would not be.
    if (tag === "TABLE") { return paragraph(escapeText(el.textContent), indent); }
    // A <div> holding blocks is a container, not a paragraph — which is what
    // a contenteditable produces constantly: pressing Enter on a list wrapped
    // it in one, and reading it as a paragraph ran «uno» and «dos» together
    // into a single word.
    if (Array.prototype.some.call(el.children, function (child) { return BLOCKS[child.tagName]; })) {
      return serialize(el, indent);
    }
    return paragraph(inline(el), indent);
  }

  function serialize(root, indent) {
    indent = indent || "";
    var blocks = [];
    var loose = [];

    function flush() {
      if (!loose.length) { return; }
      var text = loose.map(inlineOne).join("");
      var para = paragraph(text, indent);
      if (para) { blocks.push(para); }
      loose = [];
    }

    Array.prototype.forEach.call(root.childNodes, function (node) {
      if (node.nodeType === 1 && DROP[node.tagName]) {
        return;
      }
      if (node.nodeType === 1 && BLOCKS[node.tagName]) {
        flush();
        var out = blockFor(node, indent);
        if (out.trim()) { blocks.push(out); }
      } else {
        loose.push(node);          // text sitting straight in the box
      }
    });
    flush();
    return blocks.join("\n\n");
  }

  // Exposed for the test suite, which runs this file in node and compares the
  // markdown it produces against a table of documents. A browser never reads
  // this line.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { serialize: serialize };
  }

  // --- the toolbar ---------------------------------------------------------

  var TOOLS = {
    h2:     { label: "T1", title: "Título", block: "<h2>" },
    h3:     { label: "T2", title: "Subtítulo", block: "<h3>" },
    h4:     { label: "T3", title: "Apartado", block: "<h4>" },
    bold:   { label: "N", title: "Negrita", cmd: "bold" },
    italic: { label: "I", title: "Cursiva", cmd: "italic" },
    strike: { label: "S", title: "Tachado", cmd: "strikeThrough" },
    ul:     { label: "•", title: "Lista", cmd: "insertUnorderedList" },
    ol:     { label: "1.", title: "Lista numerada", cmd: "insertOrderedList" },
    quote:  { label: "“”", title: "Cita", block: "<blockquote>" },
    link:   { label: "🔗", title: "Enlace", link: true },
    clear:  { label: "✕", title: "Quitar formato", clear: true }
  };
  var SETS = {
    full: ["h2", "h3", "h4", "bold", "italic", "strike", "ul", "ol", "quote", "link", "clear"],
    basic: ["bold", "italic", "link", "clear"]
  };

  function button(label, title, onClick, extra) {
    var element = document.createElement("button");
    element.type = "button";                     // never a submit inside a form
    element.className = "md-bar__button" + (extra ? " " + extra : "");
    element.title = title;
    element.textContent = label;
    element.addEventListener("click", onClick);
    return element;
  }

  function enrich(box) {
    var set = SETS[box.getAttribute("data-rich") || "basic"] || SETS.basic;
    var form = box.form;
    var surface = document.createElement("div");
    var bar = document.createElement("div");
    var seed = box.id && document.querySelector('template[data-rich-html="' + box.id + '"]');

    surface.className = "rich prose";
    surface.contentEditable = "true";
    surface.setAttribute("role", "textbox");
    surface.setAttribute("aria-multiline", "true");
    if (box.id) { surface.id = box.id + "-rich"; }

    // A <label for> does not reach a contenteditable, so the label is wired by
    // hand and the surface says what it is to a screen reader.
    var label = box.id && document.querySelector('label[for="' + box.id + '"]');
    if (label) {
      if (!label.id) { label.id = box.id + "-label"; }
      surface.setAttribute("aria-labelledby", label.id);
      label.addEventListener("click", function () { surface.focus(); });
    }

    if (seed && seed.innerHTML.trim()) {
      surface.innerHTML = seed.innerHTML;
    } else if (box.value.trim()) {
      // No markdown parser here: this is the fallback for a box whose template
      // is missing, and plain text is the one reading that cannot be wrong.
      box.value.split(/\n{2,}/).forEach(function (chunk) {
        var p = document.createElement("p");
        p.textContent = chunk;
        surface.appendChild(p);
      });
    }
    if (!surface.firstChild) { surface.appendChild(document.createElement("p")); }

    function sync() {
      try { box.value = serialize(surface); } catch (error) { /* last good value stands */ }
    }

    function run(tool) {
      surface.focus();
      if (tool.link) {
        var url = window.prompt("Dirección del enlace", "https://");
        if (!url || !safeHref(url)) { return; }
        document.execCommand("createLink", false, url);
      } else if (tool.block) {
        document.execCommand("formatBlock", false, tool.block);
      } else if (tool.clear) {
        document.execCommand("removeFormat", false, null);
        document.execCommand("formatBlock", false, "<p>");
      } else {
        document.execCommand(tool.cmd, false, null);
      }
      sync();
    }

    bar.className = "md-bar";
    set.forEach(function (name) {
      var tool = TOOLS[name];
      bar.appendChild(button(tool.label, tool.title, function () { run(tool); }));
    });

    // The escape hatch. It is how anybody writes a table, or anything else the
    // buttons do not offer, and it is the way out if this editor ever mangles
    // something. Going back renders through the server, for the same reason
    // opening does: there is no markdown parser here.
    var raw = false;
    var swap = button("Markdown", "Escribir en markdown", function () {
      if (!raw) {
        sync();
        surface.hidden = true;
        box.hidden = false;
        box.focus();
        raw = true;
        swap.textContent = "Con formato";
        return;
      }
      var token = form && form.querySelector("input[name=csrf_token]");
      var data = new FormData();
      data.append("body", box.value);
      data.append("csrf_token", token ? token.value : "");
      fetch("/comunidad/previsualizar", {
        method: "POST", body: data, credentials: "same-origin"
      }).then(function (response) {
        return response.ok ? response.json() : null;
      }).then(function (result) {
        // Rendered by the same function the site renders with, raw HTML
        // disabled, so what comes back carries no markup of anybody else's.
        if (result) { surface.innerHTML = result.html; }
        surface.hidden = false;
        box.hidden = true;
        raw = false;
        swap.textContent = "Markdown";
      }).catch(function () {
        // Stay in markdown rather than lose what is in the box.
      });
    }, "md-bar__button--wide");
    bar.appendChild(swap);

    var required = box.required;
    box.required = false;      // a hidden required field refuses to submit and
    box.hidden = true;         // reports it on an element nobody can see
    box.parentNode.insertBefore(bar, box);
    box.parentNode.insertBefore(surface, box);

    surface.addEventListener("input", sync);
    surface.addEventListener("blur", sync);
    // A paste arrives as text: no stylesheet, no script, no forty nested spans,
    // and what lands looks like what was typed.
    surface.addEventListener("paste", function (event) {
      event.preventDefault();
      var text = (event.clipboardData || window.clipboardData).getData("text/plain");
      document.execCommand("insertText", false, text);
    });

    if (form) {
      form.addEventListener("submit", function (event) {
        sync();
        if (required && !box.value.trim()) {
          event.preventDefault();
          surface.focus();
          surface.classList.add("rich--empty");
        }
      });
    }
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

  // --- the brand preview --------------------------------------------------
  //
  // Setting the custom properties on one element, from the colour pickers above
  // it. A DOM write rather than a `style=""` attribute in the template, which
  // is what makes it work under a policy with no 'unsafe-inline' — and it is
  // also why the preview exists at all: the Marca screen itself is deliberately
  // rendered in the factory colours, so this block is the only place an admin
  // sees their own.
  function brandPreview() {
    var target = document.querySelector("[data-brand-preview-target]");
    if (!target) { return; }
    var pickers = document.querySelectorAll("[data-brand-preview] [data-token]");

    function paint() {
      Array.prototype.forEach.call(pickers, function (picker) {
        target.style.setProperty("--" + picker.getAttribute("data-token"),
                                 picker.value);
      });
    }

    Array.prototype.forEach.call(pickers, function (picker) {
      picker.addEventListener("input", paint);
    });
    paint();
  }

  document.addEventListener("DOMContentLoaded", function () {
    // Tags rather than inline styles: <b>, not <span style="font-weight:700">,
    // which the serializer would have to guess at. Harmless where the whole
    // command set is unsupported, since nothing below runs then either.
    try { document.execCommand("styleWithCSS", false, false); } catch (error) { /* older engines */ }
    Array.prototype.forEach.call(
      document.querySelectorAll("textarea[data-markdown]"), enrich);
    brandPreview();

    document.addEventListener("change", function (event) {
      var input = event.target;
      if (input.tagName === "INPUT" && input.type === "file" &&
          (input.accept || "").indexOf("image") !== -1) {
        previewFiles(input);
      }
    });
  });
})();
