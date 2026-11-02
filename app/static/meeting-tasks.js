// Meeting tasks (meeting.html): fields grow with their text, a title has no line breaks, a changed deadline shows its
// weekday. app.js replaces the form with a fresh copy on live updates, so everything works through delegation.
(() => {
  const WEEKDAYS = ['вс', 'пн', 'вт', 'ср', 'чт', 'пт', 'сб'];
  const grows = typeof CSS !== 'undefined' && CSS.supports && CSS.supports('field-sizing', 'content');
  const fit = area => {  // browsers without field-sizing (Firefox, older Safari)
    if (grows || !area.isConnected || !area.offsetParent) return;
    area.style.height = 'auto';
    area.style.height = `${area.scrollHeight + area.offsetHeight - area.clientHeight}px`;
  };
  const fitAll = () => { if (!grows) document.querySelectorAll('#meeting-tasks textarea').forEach(fit); };
  const inTasks = (target, selector) => target instanceof Element && target.matches(selector) && target.closest('#meeting-tasks');

  document.addEventListener('input', event => {
    const area = event.target;
    if (!inTasks(area, 'textarea')) return;
    if (area.matches('[data-tk-title]') && /[\r\n]/.test(area.value)) {  // a pasted title with line breaks
      const caret = area.selectionStart;
      area.value = area.value.replace(/\s*[\r\n]+\s*/g, ' ');
      area.setSelectionRange(Math.min(caret, area.value.length), Math.min(caret, area.value.length));
    }
    fit(area);
  });
  document.addEventListener('keydown', event => {  // Enter in a title neither breaks the line nor submits the form
    if (event.key === 'Enter' && inTasks(event.target, '[data-tk-title]')) event.preventDefault();
  });
  document.addEventListener('change', event => {
    const input = event.target;
    if (!inTasks(input, '[data-tk-deadline]')) return;
    const note = input.closest('.tk-params') && input.closest('.tk-params').querySelector('[data-tk-deadline-note]');
    if (!note) return;
    if (note.dataset.original === undefined) note.dataset.original = note.innerHTML;
    if (input.value === input.defaultValue) { note.innerHTML = note.dataset.original; return; }  // back to the proposed date
    const day = input.value ? new Date(`${input.value}T12:00:00`) : null;
    note.textContent = day && !Number.isNaN(day.getTime()) ? `${WEEKDAYS[day.getDay()]} · вручную` : 'без срока';
  });
  document.addEventListener('toggle', event => {  // the closed «Добавить задачу» had no size to fit
    if (event.target instanceof Element && event.target.matches('.tk-new') && event.target.open) fitAll();
  }, true);
  document.addEventListener('sysai:region-updated', fitAll);
  let width = window.innerWidth;
  window.addEventListener('resize', () => {
    if (window.innerWidth === width) return;
    width = window.innerWidth;
    fitAll();
  });
  fitAll();
})();
