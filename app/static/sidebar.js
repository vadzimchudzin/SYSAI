// The left menu: fold a group, fold the whole menu into a rail. The choice is kept in cookies, so the server draws the
// menu as it was left and nothing jumps on the next page.
(function () {
  var side = document.getElementById('side');
  if (!side) return;
  function keep(name, value) { document.cookie = name + '=' + value + '; path=/; max-age=31536000; samesite=lax'; }
  side.addEventListener('click', function (e) {
    var head = e.target.closest('[data-nav-toggle]');
    if (head) {
      var group = head.closest('.nav-group');
      var open = !group.classList.contains('open');
      group.classList.toggle('open', open);
      head.setAttribute('aria-expanded', open ? 'true' : 'false');
      var keys = Array.prototype.map.call(side.querySelectorAll('.nav-group.open'), function (g) { return g.getAttribute('data-nav-group'); });
      keep('sysai_nav', keys.length ? keys.join('.') : 'none');
      return;
    }
    if (e.target.closest('[data-side-mini]')) {
      var mini = !document.documentElement.classList.contains('side-mini');
      document.documentElement.classList.toggle('side-mini', mini);
      keep('sysai_side', mini ? 'mini' : 'full');
    }
  });
})();
