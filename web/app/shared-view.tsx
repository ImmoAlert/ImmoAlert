import {useEffect,useState} from 'react';
import {Loader2} from 'lucide-react';
import {apiFetch} from './api';
import type {Listing} from './dashboard';
import {comparisonRows} from './comparison-data';
import ProgressiveImage from './progressive-image';

export default function SharedView({token}:{token:string}){
 const [data,setData]=useState<{title:string;expires_at:string;items:Listing[]}|null>(null),[error,setError]=useState('');
 useEffect(()=>{const controller=new AbortController();void(async()=>{try{const response=await apiFetch('/api/shared/'+encodeURIComponent(token),{signal:controller.signal,cache:'no-store'}),result=await response.json();if(!response.ok)throw new Error(result.error||'Ce lien est indisponible.');setData(result)}catch(reason){if(!controller.signal.aborted)setError((reason as Error).message)}})();return()=>controller.abort()},[token]);
 return <main id="main-content" className="main shared-view"><div className="page-heading"><div><h1>{data?.title||'Comparatif partagé'}</h1><p>Lecture seule. {data&&`Valable jusqu’au ${new Date(data.expires_at).toLocaleDateString('fr-FR')}.`}</p></div><a className="button outline" href="/">Découvrir ImmoAlert</a></div>
  {error&&<p className="member-error" role="alert">{error}</p>}{!data&&!error&&<p role="status"><Loader2 className="spin" size={17}/>Chargement…</p>}
  {data&&<div className="comparison-scroll" tabIndex={0} aria-label="Tableau comparatif"><table className="comparison-table"><thead><tr><th scope="col">Les critères</th>{data.items.map(item=><th scope="col" key={item.id}><div className="comparison-photo">{item.images[0]&&<ProgressiveImage src={item.images[0]} alt={item.title}/>}</div><span>{item.title}</span></th>)}</tr></thead><tbody>{comparisonRows.filter(row=>!/trajet|Stationnement|Accessibilité/.test(row.label)).map(row=><tr key={row.label}><th scope="row">{row.label}</th>{data.items.map(item=><td key={item.id}>{row.value(item)}</td>)}</tr>)}</tbody></table></div>}
 </main>;
}
