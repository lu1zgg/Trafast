// Trafast 管理后台的少量交互：危险操作二次确认、复制按钮。CSP 只允许加载本文件，不允许内联脚本。
document.addEventListener("submit", function (e) {
  var form = e.target;
  var msg = form.getAttribute("data-confirm");
  if (msg && !window.confirm(msg)) {
    e.preventDefault();
  }
});

document.addEventListener("click", function (e) {
  var btn = e.target.closest("[data-copy]");
  if (!btn) return;
  var input = document.getElementById(btn.getAttribute("data-copy"));
  if (!input) return;
  input.select();
  var done = function () {
    var old = btn.textContent;
    btn.textContent = "已复制";
    setTimeout(function () { btn.textContent = old; }, 1500);
  };
  if (navigator.clipboard) {
    navigator.clipboard.writeText(input.value).then(done, function () { document.execCommand("copy"); done(); });
  } else {
    document.execCommand("copy");
    done();
  }
});
