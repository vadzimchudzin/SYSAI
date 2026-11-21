// «Что выбрано»: the decisions of a project as one tree — the scope, under it the controller (its program, the
// language of the program, the style), the panel (its program and the pictures of the screens), the link, the
// compilation and the documents. Every value comes from pipeline.json (what the agent wrote down) or from the
// environment the window worked out of it (apps.environment): the tree never makes a value up. A decision not written
// down yet is «не выбран» and names the question to ask (`ask` — a key of prompts.decide). Pure: the tests import it.

const STATE = { todo: 'не начата', work: 'идёт', done: 'готова', problem: 'нужен инженер' };
const DOCS = { todo: 'не начаты', work: 'идут', done: 'готовы', problem: 'нужен инженер' };
const SCOPE_WHY = {
  full: 'ПЛК → компиляция → экраны → документы',
  plc: 'программа контроллера и компиляция, экранов нет',
  hmi: 'экраны по готовой программе ПЛК',
};

function row(key, label, value, why = '', ask = '', children = []) {
  const missing = !value;
  return { key, label, value: missing ? (ask ? 'не выбран' : '') : String(value), missing, why, ask: missing ? ask : '', children };
}

// pl — pipeline as the window has it (stages with `skipped`, facts with *_title, scope); env — apps.environment;
// pictures — paths of the picture libraries the person named among the materials.
export function decisions(pl, env = {}, pictures = []) {
  const facts = (pl && pl.facts) || {};
  const stages = (pl && pl.stages) || [];
  const stage = (key) => stages.find((s) => s.stage === key) || { state: 'todo', skipped: false };
  const scope = (pl && pl.scope) || 'full';
  const plc = env.plc || {};
  const hmi = env.hmi || {};
  const withPlc = !stage('plc').skipped;
  const withHmi = !stage('hmi').skipped;

  // the controller: its program follows from the brand; the language — from what the program accepts and what the
  // generator writes; the style — from a project of the past taken apart
  const program = facts.controller && plc.program
    ? row('plc_program', 'Программа', plc.program, [plc.vendor, plc.serves].filter(Boolean).join(': '))
    : row('plc_program', 'Программа', '', 'определится по марке контроллера');
  const only = plc.languages && plc.languages.length === 1 && plc.writes && plc.writes.length === 1 ? plc.writes[0] : '';
  const language = row('language', 'Язык', facts.language_title || (only ? only.toUpperCase() : ''),
    plc.languageNote || '', 'language');
  const style = row('style', 'Стиль', facts.style, facts.style ? 'по разобранному проекту прошлых лет' : '', 'style');
  const controller = row('controller', 'Контроллер', facts.controller, plc.serves && facts.controller ? plc.serves : '', 'controller',
    withPlc ? [program, language, style] : [program]);

  // the panel: its program follows from the brand, the screens are drawn from the indexed picture pack
  const screens = facts.panel && hmi.program
    ? row('hmi_program', 'Экраны', hmi.program, [hmi.vendor, hmi.serves].filter(Boolean).join(': '))
    : row('hmi_program', 'Экраны', '', 'определится по марке панели');
  const pics = row('pictures', 'Картинки', pictures.length ? pictures.join(', ') : 'Хранилище SYSAI · «ОПО»',
    'картинки механизмов по состояниям — из проиндексированного пака');
  const panelValue = facts.panel ? [facts.panel, facts.resolution].filter(Boolean).join(' · ') : '';
  const panel = row('panel', 'Панель', panelValue, hmi.serves && facts.panel ? hmi.serves : '', 'panel', withHmi ? [screens, pics] : []);

  const link = row('link', 'Связь', facts.link_title, env.linkWhy || 'по моделям контроллера и панели', 'link');
  const children = [controller, panel, link];
  if (withPlc && !stage('build').skipped) {
    children.push(row('build', 'Компиляция', STATE[stage('build').state], plc.program ? `в ${plc.program}` : 'в среде контроллера'));
  }
  children.push(row('docs', 'Документы', DOCS[stage('docs').state], 'руководство и паспорт со снимками экранов'));
  return row('scope', 'Объём', facts.scope_title, SCOPE_WHY[scope] || '', 'scope', children);
}

// The card of the stage «Компиляция»: shown while it is the stage in hand (next, in work or waiting for the engineer)
// and the scope has it. -> null | {state, program, open: {id, title, file} | null}
export function compileCard(pl, env = {}) {
  const stages = (pl && pl.stages) || [];
  const build = stages.find((s) => s.stage === 'build');
  if (!build || build.skipped || build.state === 'done') return null;
  if (!(build.state === 'work' || build.state === 'problem' || (pl && pl.next === 'build'))) return null;
  const plc = env.plc || {};
  return {
    state: build.state,
    program: plc.program || 'среда контроллера',
    open: plc.ready && plc.id ? { id: plc.id, title: plc.program, file: plc.file } : null,
  };
}

// «Работа до документов сделана»: у объёма «только ПЛК» это компиляция, у полного и «только экраны» — экраны.
// Считаем по последнему не пропущенному объёмом этапу до документов: так «только ПЛК» предлагает документы после
// сборки, а не ждёт экранов, которых в этом объёме нет.
export function workBeforeDocs(pl) {
  const stages = (pl && pl.stages) || [];
  const made = stages.filter((s) => s.stage !== 'docs' && !s.skipped);
  return made.length > 0 && made[made.length - 1].state === 'done';
}
