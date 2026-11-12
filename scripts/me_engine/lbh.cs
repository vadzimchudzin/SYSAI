using System; using System.Runtime.Remoting.Proxies; using System.Runtime.Remoting.Messaging; using System.Runtime.Remoting; using System.IO; using System.Reflection; using System.Linq; using System.Collections.Generic; using System.Xml;
using _3S.CoDeSys.Core.Components; using _3S.CoDeSys.Core; using _3S.CoDeSys.Core.Objects;
class H {
  static bool V = Environment.GetEnvironmentVariable("H_VERBOSE") != null;
  const string ROOT = @"C:\ME\LogicBuilder";
  static Assembly Resolve(object s, ResolveEventArgs e) {
    var an = new AssemblyName(e.Name); string n = an.Name;
    if (n.EndsWith(".resources")) return null;
    string lac = Path.Combine(ROOT, "LacBinaries");
    foreach (var gac in Directory.GetDirectories(lac)) {
      string d = Path.Combine(gac, n);
      if (!Directory.Exists(d)) continue;
      string best = null;
      foreach (var vd in Directory.GetDirectories(d)) {
        string f = Path.Combine(vd, n + ".dll"); if (!File.Exists(f)) continue;
        if (an.Version != null && Path.GetFileName(vd).StartsWith(an.Version + "_")) { best = f; break; }
        best = f;
      }
      if (best != null) { return Assembly.LoadFrom(best); }
    }
    foreach (var d in new[]{ Path.Combine(ROOT,"Common"), @"C:\ME" }) { string f = Path.Combine(d, n + ".dll"); if (File.Exists(f)) return Assembly.LoadFrom(f); }
    if (V) Console.Error.WriteLine("UNRESOLVED " + e.Name);
    return null;
  }
  [STAThread]
  static int Main(string[] a) {
    AppDomain.CurrentDomain.AssemblyResolve += Resolve;
    AppDomain.CurrentDomain.UnhandledException += (s, e) => Console.Error.WriteLine("UNHANDLED " + e.ExceptionObject);
    try { Console.SetOut(new StreamWriter(Console.OpenStandardOutput(), new System.Text.UTF8Encoding(false)) { AutoFlush = true }); } catch (Exception) { }
    try { return Run(a); } catch (Exception ex) { Console.WriteLine("FAILED " + ex); return 1; }
  }
  static int Run(string[] a) {
    ComponentManager.SetRootFolder(ROOT);
    if (V) Console.WriteLine("cachefile=" + ComponentManager.CalculatePlugInCacheFile(ROOT, false));
    var cm = new ComponentManager5();
    bool rebuilt;
    int last = -1;
    ComponentManager.Singleton.InitializePlugInCache((name, i, n) => { if (i != last && i % 25 == 0 && n > 0) Console.WriteLine("building plug-in cache " + i + "/" + n + " " + name); last = i; }, out rebuilt);
    if (rebuilt) Console.WriteLine("plugin cache was rebuilt");
    
    var c = ComponentManager.Singleton;
    Guid[] plugs = c.PlugInCache.GetPlugIns();
    
    var blocked = new HashSet<Guid>();
    var doc = new XmlDocument(); doc.Load(Path.Combine(ROOT, "PluginConfiguration.xml"));
    foreach (XmlNode nd in doc.SelectNodes("PluginConfiguration/BlockedPlugin")) { if (nd.Attributes["Version"] == null && nd.Attributes["IfPluginExists"] == null) blocked.Add(new Guid(nd.Attributes["Guid"].Value)); else if (V) Console.WriteLine("cond-block " + nd.OuterXml); }
    var profile = new Profile();
    foreach (var x in (Environment.GetEnvironmentVariable("H_EXCLUDE") ?? "").Split(new[]{','}, StringSplitOptions.RemoveEmptyEntries)) blocked.Add(new Guid(x));
    string exf = @"C:\t\exclude.txt";
    if (File.Exists(exf)) foreach (var l in File.ReadAllLines(exf)) { var t = l.Trim(); if (t.Length >= 36 && !t.StartsWith("#")) blocked.Add(new Guid(t.Substring(0, 36))); }
    string mode = Environment.GetEnvironmentVariable("H_MODE") ?? "3s";
    int inc = 0; string incf = @"C:\t\include.txt"; string incs = (Environment.GetEnvironmentVariable("H_INCLUDE") ?? "") + "," + (File.Exists(incf) ? string.Join(",", File.ReadAllLines(incf).Select(l => l.Trim()).Where(l => l.Length >= 36 && !l.StartsWith("#")).Select(l => l.Substring(0, 36))) : "");
    var extraInc = new HashSet<Guid>(incs.Split(new[]{','}, StringSplitOptions.RemoveEmptyEntries).Select(x => new Guid(x)));
    foreach (var g in plugs) {
      var v = c.PlugInCache.GetPlugInVersionsFast(g).Max(); var pi = c.PlugInCache.GetPlugInInformation(new PlugInName(g, v));
      bool is3s = (pi.Company ?? "").IndexOf("3S", StringComparison.OrdinalIgnoreCase) >= 0 || (pi.Company ?? "").IndexOf("CODESYS", StringComparison.OrdinalIgnoreCase) >= 0;
      bool use = !blocked.Contains(g) && (mode == "all" || is3s || extraInc.Contains(g));
      if (Environment.GetEnvironmentVariable("H_LIST") != null) Console.WriteLine("PLUGIN " + (use ? "+" : "-") + " " + g + " " + v + " | " + pi.Title + " | " + pi.Company + " | lic=" + string.Join(";", pi.LicenseProtectionData ?? new string[0]) + " | key=" + (string.IsNullOrEmpty(pi.PlugInKey) ? "no" : "yes"));
      if (Environment.GetEnvironmentVariable("H_LIST") != null) foreach (var ti in pi.TypeInfosFast) if (!string.IsNullOrEmpty(ti.SystemItf)) Console.WriteLine("SYSTYPE " + (use ? "+" : "-") + " " + ti.SystemItf + " <- " + ti.TypeName + " {" + ti.TypeGuid + "} in " + pi.Title + " | " + pi.Company);
      if (!use) continue; inc++;
      profile.SetVersionConstraint(g, new ExactVersionConstraint(v)); }
    Console.WriteLine("lbh: plug-in cache " + plugs.Length + " plug-ins, harness profile " + inc);
    c.ProfileList.SaveProfile("Harness", profile);
    c.SetActiveProfile("Harness", profile);
    
    c.CreateSystemInstances((pn, tg, i, n) => { if (V) Console.WriteLine("sysinst " + i + "/" + n + " " + tg + " " + (pn == null ? "" : pn.ToString())); });
    
    
    if (a.Length == 0) return 0;
    var eng = SystemInstances.Engine; var om = SystemInstances.ObjectMgr;
    try { ((IEngine2)eng).SetMessageService((IMessageService)new Stub(typeof(IMessageService), "MessageService").GetTransparentProxy()); } catch (Exception ex) { Console.WriteLine("SetMessageService failed: " + ex.Message); }
    try { object ms = SystemInstances.MessageStorage; bool hooked = false;
      foreach (var it in ms.GetType().GetInterfaces()) { var ev = it.GetEvent("MessageAdded"); if (ev == null) continue; ev.AddEventHandler(ms, Delegate.CreateDelegate(ev.EventHandlerType, typeof(H).GetMethod("OnMsg", BindingFlags.Static | BindingFlags.NonPublic))); hooked = true; break; }
      if (!hooked) Console.WriteLine("message hook: no MessageAdded event"); } catch (Exception ex) { Console.WriteLine("message hook failed: " + ex.Message); }
    if (Environment.GetEnvironmentVariable("H_NOENGINEINIT") == null) { try { eng.Initialize(); Console.WriteLine("engine initialized"); } catch (Exception ex) { Console.WriteLine("ENGINE INIT FAILED " + ex); } }
    int rc = 0; string save = null; bool dump = false; string script = null; var sargs = new List<string>(); var files = new List<string>();
    for (int k = 0; k < a.Length; k++) { if (a[k] == "--save") save = a[++k]; else if (a[k] == "--dump") dump = true; else if (a[k] == "--script") { script = a[++k]; while (k + 1 < a.Length) sargs.Add(a[++k]); } else files.Add(a[k]); }
    foreach (var path in files) rc |= Open(om, path, save, dump);
    if (script != null) rc |= RunScript(script, sargs);
    Console.Out.Flush();
    Environment.Exit(rc);
    return rc;
  }
  static void OnMsg(object cat, object msg) { try { dynamic c = cat, m = msg; Console.WriteLine("MSG [" + m.Severity + "] " + (cat == null ? "" : c.Text + ": ") + m.Text); } catch (Exception ex) { Console.WriteLine("MSG ? " + msg + " (" + ex.Message + ")"); } }
  static int RunScript(string script, List<string> sargs) {
    try {
      Type itf = AppDomain.CurrentDomain.GetAssemblies().Select(x => x.GetType("_3S.CoDeSys.ScriptEngine.IScriptEngine", false)).First(t => t != null);
      object se = typeof(ComponentManager).GetMethods().First(m => m.Name == "GetSystemInstance" && m.GetParameters().Length == 0).MakeGenericMethod(itf).Invoke(ComponentManager.Singleton, null);
      object ex = itf.GetMethod("CreateScriptExecutor", Type.EmptyTypes).Invoke(se, null);
      Type exi = itf.Assembly.GetType("_3S.CoDeSys.ScriptEngine.IScriptExecutor", true);
      Type ex3 = itf.Assembly.GetType("_3S.CoDeSys.ScriptEngine.IScriptExecutor3", false);
      try { var argl = (IList<object>)ex3.GetProperty("ScriptArguments").GetValue(ex, null); argl[0] = script; foreach (var x in sargs) argl.Add(x); } catch (Exception e) { Console.WriteLine("script args not set: " + e.Message); }
      exi.GetMethod("LoadDrivers", Type.EmptyTypes).Invoke(ex, null);
      exi.GetMethod("ExecuteFile", new[]{ typeof(string) }).Invoke(ex, new object[]{ script });
      Console.WriteLine("SCRIPT DONE " + script);
      return 0;
    } catch (Exception e) {
      var b = e; while (b is TargetInvocationException && b.InnerException != null) b = b.InnerException;
      Console.WriteLine("SCRIPT FAILED " + (b.Data.Contains("_3S.CoDeSys.ScriptEngine.Formatted") ? b.Data["_3S.CoDeSys.ScriptEngine.Formatted"] : b.ToString())); return 8; }
  }
  class Stub : RealProxy, IRemotingTypeInfo {
    string n; public Stub(Type t, string name) : base(t) { n = name; }
    public bool CanCastTo(Type t, object o) { return t.IsInterface; }
    public string TypeName { get { return n; } set { } }
    public override IMessage Invoke(IMessage m) {
      var c = (IMethodCallMessage)m; var mi = (MethodInfo)c.MethodBase;
      string argtxt = string.Join(" | ", (c.Args ?? new object[0]).Select(x => x == null ? "null" : (x is object[] ? "[" + string.Join(",", ((object[])x).Select(y => "" + y)) + "]" : x.ToString())));
      if (argtxt.Length > 1500) argtxt = argtxt.Substring(0, 1500) + "...";
      Console.WriteLine("SERVICE " + n + "." + mi.Name + "(" + argtxt + ")");
      object ret = null; if (mi.ReturnType != typeof(void) && mi.ReturnType.IsValueType) ret = Activator.CreateInstance(mi.ReturnType);
      return new ReturnMessage(ret, c.Args, c.ArgCount, c.LogicalCallContext, c);
    }
  }
  static string WhoProvides(string msg) {
    try { var m = System.Text.RegularExpressions.Regex.Match(msg, "[0-9a-fA-F]{8}-[0-9a-fA-F-]{27}"); if (!m.Success) return ""; Guid tg = new Guid(m.Value); var pc = ComponentManager.Singleton.PlugInCache;
      foreach (var g in pc.GetPlugIns()) foreach (var v in pc.GetPlugInVersionsFast(g)) { var pi = pc.GetPlugInInformation(new PlugInName(g, v)); foreach (var ti in pi.TypeInfosFast) if (ti.TypeGuid == tg) return " (provided by plug-in '" + pi.Title + "' " + g + " " + v + " [" + pi.Company + "], " + ti.TypeName + " - not in harness profile)"; }
      return " (no plug-in in the plug-in cache provides this type)"; } catch (Exception ex) { return " (lookup failed: " + ex.Message + ")"; } }
  static string Dump(object o, int depth) {
    var sb = new System.Text.StringBuilder(); string ind = new string(' ', depth * 2 + 4);
    foreach (var pn in new[]{ "Interface", "Implementation" }) { try {
      var p = o.GetType().GetProperty(pn); if (p == null) continue; object v = p.GetValue(o, null); if (v == null) continue;
      string text = null; var tp = v.GetType().GetProperty("TextDocument"); object td = tp != null ? tp.GetValue(v, null) : null;
      if (td != null) { var xp = td.GetType().GetProperty("Text"); if (xp != null) text = xp.GetValue(td, null) as string; }
      sb.Append("\n" + ind + "." + pn + " : " + v.GetType().Name + (text == null ? "" : " text(" + text.Length + " chars):\n" + string.Join("\n", text.Replace("\r", "").Split('\n').Select(l => ind + "  | " + l))));
    } catch (Exception ex) { sb.Append("\n" + ind + "." + pn + " dump error " + ex.Message); } }
    return sb.ToString();
  }
  static int Open(IObjectManager om, string path, string save, bool dump) {
    Console.WriteLine("=== " + path);
    string work = Path.Combine(Path.GetTempPath(), "lbh_" + Guid.NewGuid().ToString("N")); Directory.CreateDirectory(work);
    int h;
    try {
      using (var fs = new FileStream(path, FileMode.Open, FileAccess.Read)) { h = om.LoadProject(fs, path, work); om.FinishLoadProject(h); }
    } catch (Exception ex) { Console.WriteLine("LOAD FAILED " + ex); return 2; }
    Guid[] all = om.GetAllObjects(h);
    Console.WriteLine("OPENED " + path + " handle=" + h + " objects=" + all.Length);
    var kids = new Dictionary<Guid, List<IMetaObjectStub>>(); int bad = 0;
    var stubs = new List<IMetaObjectStub>();
    foreach (var g in all) { try { var st = om.GetMetaObjectStub(h, g); stubs.Add(st); List<IMetaObjectStub> l; if (!kids.TryGetValue(st.ParentObjectGuid, out l)) kids[st.ParentObjectGuid] = l = new List<IMetaObjectStub>(); l.Add(st); } catch (Exception ex) { bad++; Console.WriteLine("STUB FAILED " + g + " " + ex.Message); } }
    var seen = new HashSet<Guid>(); int incomplete = 0, unknown = 0, broken = 0;
    Action<Guid, int> walk = null;
    walk = (parent, depth) => { List<IMetaObjectStub> l; if (!kids.TryGetValue(parent, out l)) return;
      foreach (var st in l.OrderBy(x => x.Index)) { seen.Add(st.ObjectGuid);
        string t = "?"; string err = null;
        string extra = "";
        try { var mo = om.GetObjectToRead(h, st.ObjectGuid); t = mo.Object == null ? "null" : mo.Object.GetType().FullName;
          try { dynamic dm = mo; if ((bool)dm.DeserializedIncompletely) { incomplete++; dynamic di = dm.IncompleteDeserializationInfo; extra += "  INCOMPLETE(unknownObject=" + di.UnknownObject + " type=" + di.Type + " value=" + di.UnknownSerializableValueName + " warn=" + di.Warning + " cancel=" + di.Cancellation + ")"; } } catch (Exception) { }
          if (mo.Object != null && mo.Object.GetType().Name == "UnknownObject") { unknown++; var f = mo.Object.GetType().GetField("_exception", BindingFlags.NonPublic | BindingFlags.Instance); var ue = f == null ? null : f.GetValue(mo.Object) as Exception; if (ue != null) { string um = ue.Message.Replace("\r", " ").Replace("\n", " "); if (um.Length > 300) um = um.Substring(0, 300) + "...";
              if (ue.GetType().Name == "TypeNotFoundException") { extra += "  UNKNOWN-TYPE " + um + WhoProvides(um); } else { broken++; extra += "  OBJECT BROKEN " + ue.GetType().Name + ": " + um; } } }
          if (dump && mo.Object != null) extra += Dump(mo.Object, depth);
        } catch (Exception ex) { bad++; err = ex.GetType().Name + ": " + ex.Message; }
        Console.WriteLine(new string(' ', depth * 2) + st.Name + "  [" + (st.ObjectType == null ? "?" : st.ObjectType.FullName) + "] " + st.ObjectGuid + (err != null ? "  READ FAILED " + err : (st.ObjectType != null && t != st.ObjectType.FullName ? "  actual=" + t : "")) + extra);
        walk(st.ObjectGuid, depth + 1); } };
    walk(Guid.Empty, 0);
    foreach (var st in stubs) if (!seen.Contains(st.ObjectGuid)) Console.WriteLine("ORPHAN " + st.Name + " " + st.ObjectGuid + " parent=" + st.ParentObjectGuid);
    Console.WriteLine("SUMMARY objects=" + all.Length + " failed=" + bad + " broken=" + broken + " unknown=" + unknown + " incomplete=" + incomplete + " types: " + string.Join(", ", stubs.GroupBy(x => x.ObjectType == null ? "?" : x.ObjectType.Name).OrderByDescending(x => x.Count()).Select(x => x.Key + "=" + x.Count())));
    if (save != null) { try { using (var o = new FileStream(save, FileMode.Create, FileAccess.ReadWrite)) om.SaveProject(h, o, save); Console.WriteLine("SAVED " + save + " bytes=" + new FileInfo(save).Length); } catch (Exception ex) { Console.WriteLine("SAVE FAILED " + ex); bad++; } }
    try { om.CloseProject(h); } catch (Exception ex) { Console.WriteLine("CLOSE FAILED " + ex.Message); }
    return bad + broken == 0 ? 0 : 4;

  }
}
