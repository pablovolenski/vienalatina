// The three share buttons that cannot be plain links.
//
// WhatsApp, Facebook, Bluesky and Mastodon all accept a URL that opens their
// own composer, so those are anchors in share.html and work with this file
// absent. These three cannot be:
//
//   Signal     has no web intent at all. It is a share *target* in the
//              operating system's own sheet and nowhere else.
//   Instagram  accepts no link from anybody, by design. Every site with an
//              Instagram share button is really offering a copied link.
//   Copiar     is the fallback for every case the two above do not cover,
//              including a desktop with no share sheet.
//
// All three are `hidden` in the markup and shown here, so a reader without
// scripting sees four buttons that work rather than seven of which three do
// nothing.

(function () {
  "use strict";

  function copy(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
    // Older engines, and any page not served over https: a textarea, selected
    // and copied the way it was done before the clipboard API existed.
    return new Promise(function (resolve, reject) {
      var box = document.createElement("textarea");
      box.value = text;
      box.setAttribute("readonly", "");
      box.style.position = "fixed";
      box.style.opacity = "0";
      document.body.appendChild(box);
      box.select();
      try {
        document.execCommand("copy") ? resolve() : reject();
      } catch (error) {
        reject(error);
      }
      document.body.removeChild(box);
    });
  }

  function wire(row) {
    var url = row.getAttribute("data-url");
    var title = row.getAttribute("data-title");
    var text = row.getAttribute("data-text");
    var said = row.querySelector(".share__said");

    function announce(message) {
      if (!said) { return; }
      said.textContent = message;
      window.setTimeout(function () { said.textContent = ""; }, 2500);
    }

    function copyAndSay() {
      copy(url).then(function () {
        announce(row.getAttribute("data-copied"));
      }).catch(function () {
        // Copying can be refused outright — a permission, an engine without
        // either route. Saying nothing would look like a button that does
        // nothing, so the address is put on screen to be copied by hand.
        announce(url);
      });
    }

    // Signal and Instagram: the operating system's sheet, which lists both
    // when they are installed. There is no way to open one app in particular,
    // and pretending otherwise is what a broken button is made of.
    Array.prototype.forEach.call(row.querySelectorAll("[data-share-native]"), function (button) {
      button.hidden = false;
      button.addEventListener("click", function () {
        if (navigator.share) {
          navigator.share({ title: title, text: text, url: url }).catch(function () {
            // Dismissed, which is not a failure and needs no message.
          });
        } else {
          copyAndSay();
        }
      });
    });

    Array.prototype.forEach.call(row.querySelectorAll("[data-share-copy]"), function (button) {
      button.hidden = false;
      button.addEventListener("click", copyAndSay);
    });

    // Mastodon has no central address: the composer lives on whichever server
    // the *reader* has an account on. Asked once and kept, because asking on
    // every post would be worse than not offering it.
    var masto = row.querySelector("[data-share-mastodon]");
    if (masto) {
      masto.hidden = false;
      masto.addEventListener("click", function (event) {
        var saved = null;
        try { saved = window.localStorage.getItem("vl-mastodon"); } catch (error) { saved = null; }
        var host = saved || window.prompt(row.getAttribute("data-ask"), "mastodon.social");
        if (!host) {
          // Nothing typed: fall through to the association's own server when
          // the markup had one, and otherwise do nothing at all.
          if (masto.getAttribute("href") === "#") { event.preventDefault(); }
          return;
        }
        host = host.trim().replace(/^https?:\/\//, "").replace(/\/.*$/, "");
        if (!host) { event.preventDefault(); return; }
        try { window.localStorage.setItem("vl-mastodon", host); } catch (error) { /* private window */ }
        masto.setAttribute("href",
          "https://" + host + "/share?text=" + encodeURIComponent(text));
      });
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    Array.prototype.forEach.call(document.querySelectorAll("[data-share]"), wire);
  });
})();
