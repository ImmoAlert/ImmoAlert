import {useEffect,useState,type FormEvent} from 'react';
import {CalendarDays,KeyRound,Link2,Loader2,ShieldCheck} from 'lucide-react';
import {apiFetch,apiPath} from './api';

type Share={id:number;title:string;expires_at:string;count:number;active:boolean};
const post=async(path:string,body:object)=>{const response=await apiFetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),data=await response.json();if(!response.ok)throw new Error(data.error||'Action impossible.');return data};
export default function AccountSecurity(){
 const [current,setCurrent]=useState(''),[next,setNext]=useState(''),[code,setCode]=useState(''),[setup,setSetup]=useState<{secret:string;uri:string}|null>(null),[recovery,setRecovery]=useState<string[]>([]),[message,setMessage]=useState(''),[error,setError]=useState(''),[busy,setBusy]=useState(false),[shares,setShares]=useState<Share[]>([]),[twoFactor,setTwoFactor]=useState<boolean|null>(null);
 const loadShares=()=>apiFetch('/api/shares',{cache:'no-store'}).then(r=>r.ok?r.json():{items:[]}).then(d=>setShares(d.items||[])).catch(()=>{});
 useEffect(()=>{void loadShares();void apiFetch('/api/state',{cache:'no-store'}).then(r=>r.json()).then(d=>setTwoFactor(d.account?.two_factor??null)).catch(()=>{})},[]);
 async function run(work:()=>Promise<void>){setBusy(true);setError('');setMessage('');try{await work()}catch(reason){setError((reason as Error).message)}finally{setBusy(false)}}
 const password=(event:FormEvent)=>{event.preventDefault();void run(async()=>{const d=await post('/api/account/password',{current,new:next});setMessage(d.message);setCurrent('');setNext('')})};
 const start=()=>run(async()=>setSetup(await post('/api/account/2fa-start',{})));
 const confirm=(event:FormEvent)=>{event.preventDefault();void run(async()=>{const d=await post('/api/account/2fa-confirm',{code});setRecovery(d.recovery_codes);setSetup(null);setTwoFactor(true);setCode('');setMessage(d.message)})};
 const disable=(event:FormEvent)=>{event.preventDefault();void run(async()=>{const d=await post('/api/account/2fa-disable',{code});setTwoFactor(false);setCode('');setMessage(d.message||'Authentification à deux facteurs désactivée.')})};
 const revoke=(id:number)=>run(async()=>{const r=await apiFetch('/api/shares/'+id,{method:'DELETE'});if(!r.ok)throw new Error('Révocation impossible.');await loadShares()});
 return <section className="member-settings account-security"><div className="extra-heading"><h2><ShieldCheck size={21}/>Sécurité et partage</h2></div>
  <form className="preferences-form" onSubmit={password}><h3><KeyRound size={16}/>Changer de mot de passe</h3><label>Mot de passe actuel<input type="password" autoComplete="current-password" required value={current} onChange={e=>setCurrent(e.target.value)}/></label><label>Nouveau mot de passe (10 caractères minimum)<input type="password" autoComplete="new-password" required minLength={10} maxLength={128} value={next} onChange={e=>setNext(e.target.value)}/></label><button className="button outline" disabled={busy}>Modifier</button><p className="fine-print">Vos autres appareils seront déconnectés.</p></form>
  <div className="preferences-form"><h3>Authentification à deux facteurs</h3>
   {twoFactor===null&&<p className="fine-print">Disponible pour les comptes avec mot de passe.</p>}
   {twoFactor===false&&!setup&&<button className="button outline" disabled={busy} onClick={()=>void start()}>Activer la 2FA</button>}
   {setup&&<form onSubmit={confirm}><p>Ajoutez ce secret dans votre application d’authentification (type TOTP) : <code>{setup.secret}</code></p><p className="fine-print">Ou ouvrez : <a href={setup.uri}>lien otpauth</a></p><label>Code à 6 chiffres<input inputMode="numeric" autoComplete="one-time-code" required maxLength={8} value={code} onChange={e=>setCode(e.target.value)}/></label><button className="button primary" disabled={busy}>Confirmer</button></form>}
   {recovery.length>0&&<div role="status"><p><strong>Codes de secours</strong> — affichés une seule fois, à conserver :</p><ul>{recovery.map(c=><li key={c}><code>{c}</code></li>)}</ul></div>}
   {twoFactor&&<form onSubmit={disable}><label>Code (ou code de secours) pour désactiver<input required maxLength={16} autoComplete="one-time-code" value={code} onChange={e=>setCode(e.target.value)}/></label><button className="button outline" disabled={busy}>Désactiver la 2FA</button></form>}
  </div>
  <div className="preferences-form"><h3><CalendarDays size={16}/>Visites</h3><a className="button outline" href={apiPath('/api/calendar.ics')} download>Exporter mes visites (.ics)</a></div>
  <div className="preferences-form"><h3><Link2 size={16}/>Liens de partage</h3>{shares.length===0?<p className="fine-print">Créez un lien depuis le comparateur de vos favoris.</p>:<ul>{shares.map(s=><li key={s.id}>{s.title||`Comparatif de ${s.count} biens`} · {s.active?`jusqu’au ${new Date(s.expires_at).toLocaleDateString('fr-FR')}`:'expiré ou révoqué'} {s.active&&<button className="text-button" disabled={busy} onClick={()=>void revoke(s.id)}>Révoquer</button>}</li>)}</ul>}</div>
  {busy&&<p role="status"><Loader2 className="spin" size={15}/></p>}{message&&<p role="status">{message}</p>}{error&&<p className="member-error" role="alert">{error}</p>}
 </section>;
}
