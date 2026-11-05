/* SYSAI · organisation map: HTML views of the inspector panel (employee, department, company). */
window.SysaiOrgMapViews = function (ctx) {
  'use strict';
  const { esc, initials, deptById, deptAccent, accentOf, nodeByKey, nodeTitle, membersOf, data, root, ICON_X, ICON_CROWN } = ctx;
  const levelLabel = { full: 'Полный доступ', reviewer: 'Проверяющий', observer: 'Наблюдатель', director: 'Директор' };
  function employeeHtml(emp, node) {
    if (!emp) return '';
    const isDirector = node.classList.contains('is-director');
    const badges = [];
    if (isDirector) badges.push(`<span class="org-insp-badge gold">${ICON_CROWN} Директор</span>`);
    emp.memberships.filter(m => m.is_primary).forEach(m => badges.push(`<span class="org-insp-badge gold">★ Руководит: ${esc(deptById.get(m.department_id)?.name)}</span>`));
    if (emp.access_level && emp.access_level !== 'employee') badges.push(`<span class="org-insp-badge${emp.access_level === 'reviewer' ? ' red' : ''}">${esc(levelLabel[emp.access_level] || emp.access_level)}</span>`);
    if (!emp.memberships.length) badges.push('<span class="org-insp-badge">Без подразделения</span>');
    const rows = emp.memberships.map(m => {
      const d = deptById.get(m.department_id);
      return `<div class="org-insp-row" style="--accent:${deptAccent(m.department_id)}" data-membership="${m.department_id}">
        <span class="dot"></span>
        <div class="main"><button type="button" class="link" data-open-dept="${m.department_id}">${esc(d?.name || 'Подразделение')}</button>
          <input data-role value="${esc(m.role)}" placeholder="Роль в отделе — например, инженер" maxlength="200" aria-label="Роль в подразделении"></div>
        <button type="button" class="org-icon-btn${m.is_primary ? ' is-on' : ''}" data-toggle-head title="${m.is_primary ? 'Снять с руководства' : 'Назначить руководителем'}">★</button>
        <button type="button" class="org-icon-btn is-danger" data-remove title="Убрать из подразделения">${ICON_X}</button>
      </div>`;
    }).join('');
    const free = data.departments.filter(d => d.active && !emp.memberships.some(m => m.department_id === d.id));
    const rels = data.relations.filter(r => r.source === `employee:${emp.id}` || r.target === `employee:${emp.id}`);
    const relRows = rels.map(r => {
      const other = r.source === `employee:${emp.id}` ? r.target : r.source;
      const dir = r.source === `employee:${emp.id}` ? '→' : '←';
      return `<div class="org-insp-row"><span class="dot" style="--accent:#d9b98a"></span><div class="main"><b>${dir} ${esc(nodeTitle(nodeByKey.get(other)))}</b><small>${esc(r.label || (String(r.target).startsWith('department:') ? 'Связь отделов' : 'Подчинение'))}</small></div>
        <button type="button" class="org-icon-btn is-danger" data-del-rel="${r.id}" title="Удалить связь">${ICON_X}</button></div>`;
    }).join('');
    return `<header class="org-insp-head">
        <button type="button" class="org-insp-close" aria-label="Закрыть">×</button>
        <div class="org-insp-id"><span class="org-map-person-avatar">${esc(initials(emp.name))}</span>
          <div><span class="org-map-node-kicker">Сотрудник</span><h3>${esc(emp.name)}</h3><p>${esc(emp.position || 'Должность не указана')}</p></div></div>
        <div class="org-insp-badges">${badges.join('')}</div>
      </header>
      <div class="org-insp-body">
        <section class="org-insp-section"><h4>Подразделения <span>${emp.memberships.length}</span></h4>
          <div class="org-insp-list">${rows || '<div class="org-insp-empty">Пока не состоит ни в одном подразделении.</div>'}</div>
          ${free.length ? `<form class="org-insp-add" data-add-membership>
            <div class="row2"><select name="department_id" required aria-label="Подразделение">${free.map(d => `<option value="${d.id}">${esc(d.name)}</option>`).join('')}</select></div>
            <div class="row2"><input name="role" maxlength="200" placeholder="Роль (необязательно)"><button class="btn sm primary">Добавить</button></div>
          </form>` : ''}
          <p class="org-insp-note" style="margin-top:9px">Роль сохраняется при выходе из поля. ★ — руководитель отдела.</p>
        </section>
        ${emp.editable ? `<section class="org-insp-section"><h4>Профиль</h4>
          <form class="org-insp-form" data-profile>
            <label>Имя и фамилия<input name="name" required maxlength="200" value="${esc(emp.name)}"></label>
            <label>Должность<input name="position" maxlength="200" value="${esc(emp.position)}"></label>
            <label>Как называют на совещаниях<input name="aliases" maxlength="500" value="${esc(emp.aliases)}" placeholder="Ваня, Иван Сергеевич"></label>
            <label>Компетенции<textarea name="competencies" rows="2" maxlength="4000">${esc(emp.competencies)}</textarea></label>
            <label>Общие обязанности<textarea name="responsibilities" rows="2" maxlength="4000">${esc(emp.responsibilities)}</textarea></label>
            <label>Контекст<textarea name="context" rows="2" maxlength="4000">${esc(emp.context)}</textarea></label>
            <div class="org-insp-actions"><button class="btn sm primary">Сохранить профиль</button></div>
          </form>
        </section>` : ''}
        ${relRows ? `<section class="org-insp-section"><h4>Связи на карте <span>${rels.length}</span></h4><div class="org-insp-list">${relRows}</div></section>` : ''}
      </div>
      <footer class="org-insp-foot"><span class="muted">${emp.telegram ? 'Telegram: ' + esc(emp.telegram) : 'Telegram не привязан'}</span><a href="/employees?edit=${emp.id}">Доступы и голос →</a></footer>`;
  }
  function departmentHtml(dep) {
    if (!dep) return '';
    const members = membersOf(dep.id).sort((a, b) => {
      const pa = a.memberships.find(m => m.department_id === dep.id)?.is_primary ? 0 : 1;
      const pb = b.memberships.find(m => m.department_id === dep.id)?.is_primary ? 0 : 1;
      return pa - pb || a.name.localeCompare(b.name, 'ru');
    });
    const rows = members.map(e => {
      const m = e.memberships.find(x => x.department_id === dep.id);
      return `<div class="org-insp-row" data-member="${e.id}">
        <span class="ava">${esc(initials(e.name))}</span>
        <div class="main"><button type="button" class="link" data-open-emp="${e.id}">${esc(e.name)}</button>
          <input data-role value="${esc(m.role)}" placeholder="${esc(e.position || 'Роль в отделе')}" maxlength="200" aria-label="Роль в подразделении"></div>
        <button type="button" class="org-icon-btn${m.is_primary ? ' is-on' : ''}" data-toggle-head title="${m.is_primary ? 'Снять с руководства' : 'Назначить руководителем'}">★</button>
        <button type="button" class="org-icon-btn is-danger" data-remove title="Убрать из подразделения">${ICON_X}</button>
      </div>`;
    }).join('');
    const available = data.employees.filter(e => !e.memberships.some(m => m.department_id === dep.id));
    const head = members.find(e => e.memberships.find(m => m.department_id === dep.id)?.is_primary);
    return `<header class="org-insp-head">
        <button type="button" class="org-insp-close" aria-label="Закрыть">×</button>
        <div class="org-insp-id"><span class="org-insp-swatch"></span>
          <div><span class="org-map-node-kicker">Подразделение</span><h3>${esc(dep.name)}</h3><p>${members.length} в составе · ${head ? 'руководитель: ' + esc(head.name) : 'руководитель не назначен'}</p></div></div>
        ${dep.active ? '' : '<div class="org-insp-badges"><span class="org-insp-badge">Неактивно</span></div>'}
      </header>
      <div class="org-insp-body">
        <section class="org-insp-section"><h4>Состав <span>${members.length}</span></h4>
          <div class="org-insp-list">${rows || '<div class="org-insp-empty">В подразделении пока никого нет.</div>'}</div>
          ${dep.active ? `<div class="org-insp-add">
            <input type="search" data-pick-search placeholder="Добавить из справочника — начните вводить имя" aria-label="Поиск сотрудника">
            <div class="org-insp-pick" data-pick>${available.slice(0, 60).map(e => `<button type="button" data-pick-emp="${e.id}" style="--accent:${accentOf(nodeByKey.get('employee:' + e.id))}"><span class="ava">${esc(initials(e.name))}</span>${esc(e.name)}<small>${esc(e.position)}</small></button>`).join('') || '<div class="org-insp-empty">Все сотрудники уже в составе.</div>'}</div>
            <button type="button" class="btn sm" data-create-employee-for="${dep.id}">＋ Новый сотрудник в «${esc(dep.name)}»</button>
          </div>` : '<p class="org-insp-note">Чтобы менять состав, активируйте подразделение.</p>'}
        </section>
        <section class="org-insp-section"><h4>Настройки</h4>
          <form class="org-insp-form" data-dept-form>
            <label>Название<input name="name" required maxlength="100" value="${esc(dep.name)}"></label>
            <label>Зона ответственности<textarea name="description" rows="2" maxlength="1000">${esc(dep.description)}</textarea></label>
            <label>Регламенты: кто что согласует и подписывает<textarea name="regulations" rows="3" maxlength="10000">${esc(dep.regulations)}</textarea></label>
            <label class="check"><input type="checkbox" name="active" ${dep.active ? 'checked' : ''}> Подразделение активно</label>
            <div class="org-insp-actions"><button type="button" class="btn sm danger" data-dept-delete>Удалить</button><button class="btn sm primary">Сохранить</button></div>
          </form>
        </section>
      </div>
      <footer class="org-insp-foot"><span class="muted">Перетащите карточку сотрудника на отдел, чтобы добавить</span></footer>`;
  }
  function companyHtml() {
    const heads = data.employees.filter(e => e.memberships.some(m => m.is_primary)).length;
    // The director needs no department: he is not «без подразделения».
    const free = data.employees.filter(e => !e.memberships.length && !nodeByKey.get(`employee:${e.id}`)?.classList.contains('is-director'));
    return `<header class="org-insp-head">
        <button type="button" class="org-insp-close" aria-label="Закрыть">×</button>
        <div class="org-insp-id"><span class="org-insp-swatch" style="--accent:#d98e8a"></span>
          <div><span class="org-map-node-kicker">Компания</span><h3>${esc(nodeTitle(root))}</h3><p>${data.departments.length} подразделений · ${data.employees.length} сотрудников · ${heads} руководителей</p></div></div>
      </header>
      <div class="org-insp-body">
        <section class="org-insp-section"><h4>Подразделения <span>${data.departments.length}</span></h4><div class="org-insp-list">
          ${data.departments.map(d => `<div class="org-insp-row" style="--accent:${deptAccent(d.id)}"><span class="dot"></span><div class="main"><button type="button" class="link" data-open-dept="${d.id}">${esc(d.name)}</button><small>${membersOf(d.id).length} в составе${d.active ? '' : ' · неактивно'}</small></div></div>`).join('') || '<div class="org-insp-empty">Подразделений пока нет.</div>'}
        </div><div class="org-insp-actions" style="margin-top:10px"><button type="button" class="btn sm" data-create-department-here>＋ Подразделение</button></div></section>
        ${free.length ? `<section class="org-insp-section"><h4>Без подразделения <span>${free.length}</span></h4><div class="org-insp-list">${free.map(e => `<div class="org-insp-row"><span class="ava">${esc(initials(e.name))}</span><div class="main"><button type="button" class="link" data-open-emp="${e.id}">${esc(e.name)}</button><small>${esc(e.position || 'Должность не указана')}</small></div></div>`).join('')}</div><p class="org-insp-note" style="margin-top:9px">Перетащите карточку на отдел или откройте сотрудника и добавьте подразделение.</p></section>` : ''}
      </div>`;
  }
  return { employeeHtml, departmentHtml, companyHtml };
};
