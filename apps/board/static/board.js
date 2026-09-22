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
