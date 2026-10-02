// The signed-in strip on the public site.
//
// vienalatina.com is files on disk. They are the same files for everyone and
// they get cached, so the one thing they cannot contain is who is reading them.
// This asks the members area instead, and draws a bar above the header when the
// answer is somebody.
//
// Three decisions worth knowing before changing anything here:
//
// * **It does nothing without the hint cookie.** `vl_sesion` is set at sign-in
//   and cleared at sign-out, holds the digit 1, and exists so that a stranger
//   reading one article costs zero requests to the app. It is not trusted for
//   anything: the server decides, and a forged hint earns a request that says
//   "not signed in".
// * **Every value goes in as text, never as HTML.** A display name is whatever
//   somebody typed into a form. textContent makes that a name; innerHTML would
//   make it a script.
// * **No frameworks, no build step.** Hugo fingerprints this file and serves it
//   from our own domain, which is the only place the site loads anything from.
(function () {
  "use strict";

  if (document.cookie.indexOf("vl_sesion=1") === -1) {
    return;
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function draw(state) {
    var bar = el("div", "member-bar");
    bar.setAttribute("role", "navigation");
    bar.setAttribute("aria-label", "Área de miembros");

    var inner = el("div", "member-bar__inner");
    var links = el("ul", "member-bar__links");
    state.sections.forEach(function (section) {
      var item = document.createElement("li");
      var link = el("a", null, section.label);
      link.href = section.url;
      item.appendChild(link);
      links.appendChild(item);
    });
    inner.appendChild(links);

    var account = el("div", "member-bar__account");
    var me = el("a", "member-bar__name", state.name);
    me.href = state.profile;
    account.appendChild(me);

    // A real form, because signing out changes something and a link that
    // changes something is a link a prefetching browser will follow by itself.
    var form = document.createElement("form");
    form.method = "post";
    form.action = state.logout;
    var token = document.createElement("input");
    token.type = "hidden";
    token.name = "csrf_token";
    token.value = state.csrf;
    form.appendChild(token);
    var out = el("button", "member-bar__out", "Salir");
    out.type = "submit";
    form.appendChild(out);
    account.appendChild(form);

    inner.appendChild(account);
    bar.appendChild(inner);
    document.body.insertBefore(bar, document.body.firstChild);
    document.body.classList.add("has-member-bar");
  }

  fetch("/comunidad/sesion.json", { credentials: "same-origin" })
    .then(function (response) {
      return response.ok ? response.json() : null;
    })
    .then(function (state) {
      if (state && state.signed_in) draw(state);
    })
    .catch(function () {
      // The members area being down is not a reason for the public site to
      // report anything. No bar, no message, no console noise.
    });
})();
