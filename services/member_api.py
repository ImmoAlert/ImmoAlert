"""Authenticated member features, bounded inventory and live detail updates."""
from __future__ import annotations

import asyncio
from datetime import datetime
import json
import math
from time import perf_counter

from aiohttp import web
from sqlalchemy import select
from database import models as m,repo
from services import alert_preferences,audit,calendar_export,commute,comparison_score,extra_channels,inventory,member_notifications,performance,property_tracking,sharing,site_sync


async def _identity(request):
    from services.web_api import current_identity
    identity,_=await current_identity(request,complete=False)
    if not identity or not identity.get('user_id'):
        raise web.HTTPUnauthorized(text=json.dumps({'error':'Connectez-vous au site.'}),content_type='application/json')
    return identity


async def _viewer(request):
    """Signed-in member, or None for a public visitor; plus the rate-limit key."""
    from services.web_api import current_identity
    identity,_=await current_identity(request,complete=False)
    if identity and identity.get('user_id'):return identity,'u:'+str(identity['user_id'])
    return None,'ip:'+request.get('client_ip',request.remote or 'unknown')


async def _recheck(request,identity):
    if identity:await _identity(request)


def _result(data,status=200):
    from services.web_api import response
    return response(data,status)


def _site(request):
    from services.web_api import STATE
    return request.app[STATE]


async def _body(request):
    try:
        value=await request.json()
        if not isinstance(value,dict):raise ValueError()
        return value
    except (ValueError,TypeError):raise web.HTTPBadRequest(text=json.dumps({'error':'Requête invalide.'}),content_type='application/json') from None


def _identifier(request):
    value=request.match_info.get('identifier','')
    if not value.isdigit() or not 0<int(value)<=1000000000:raise ValueError('Annonce introuvable.')
    return int(value)


def detail_payload(row,user_id,enriching=False):
    if not row or row.removed_at is not None or row.inventory_status=='invalid':raise ValueError('Cette annonce a été supprimée du site.')
    listing=site_sync._listing(row,full=True,publications=repo.listing_publications(row.id))
    listing['price_history']=property_tracking.price_history(row.id)
    listing['freshness']=property_tracking.publication_freshness(row.id)
    if user_id is None:
        listing['is_favorite']=False;listing['tracking']=None
        return {'listing':listing,'enriching':enriching,'retry_after_ms':2000 if enriching else None,'updates_available':True}
    with repo.session_scope() as session:
        favorite=bool(session.scalar(select(m.Favorite.id).where(m.Favorite.user_id==user_id,m.Favorite.listing_id==row.id)))
        listing['is_favorite']=favorite
        listing['tracking']=property_tracking.serialize_note(session.get(m.MemberListingNote,(user_id,row.id))) if favorite else None
    return {'listing':listing,'enriching':enriching,'retry_after_ms':2000 if enriching else None,'updates_available':True}


async def location_area(request):
    identity,key=await _viewer(request)
    _site(request).check_rate('location-area:'+key,30,60)
    try:
        row=await asyncio.to_thread(repo.get_listing_by_id,_identifier(request))
        if not row or row.removed_at is not None or row.inventory_status=='invalid':raise ValueError('Annonce introuvable.')
        from services.location_area import for_listing
        started=perf_counter()
        area=await for_listing(row)
        performance.elapsed("location_area",started)
        await _recheck(request,identity)
        return _result({'area':area})
    except ValueError as exc:return _result({'error':str(exc)},404)


async def inventory_page(request):
    identity,key=await _viewer(request);site=_site(request)
    if identity is None and (request.query.get('favorites','0')!='0' or request.query.get('filter_id')):
        return _result({'error':'Connectez-vous au site.'},401)
    site.check_rate('inventory:'+key,100 if identity else 60,60)
    started=perf_counter()
    try:data=await asyncio.to_thread(inventory.listing_page,identity['user_id'] if identity else None,request.query)
    except ValueError as exc:return _result({'error':str(exc)},400)
    performance.elapsed('inventory_api',started)
    return _result(data)


async def tracking_page(request):
    identity=await _identity(request)
    _site(request).check_rate("tracking:"+str(identity["user_id"]),100,60)
    from services.tracking_overview import listing_page
    try:return _result(await asyncio.to_thread(listing_page,identity["user_id"],request.query))
    except ValueError as exc:return _result({"error":str(exc)},400)
    except PermissionError as exc:return _result({"error":str(exc)},403)


async def search_counts(request):
    identity=await _identity(request)
    _site(request).check_rate('search-counts:'+str(identity['user_id']),20,60)
    def counts():
        return {'counts':{str(search.id):inventory.listing_page(identity['user_id'],{'filter_id':str(search.id),'limit':'1'})['total'] for search in repo.get_user_filters(identity['user_id'])},'generated_at':datetime.utcnow().isoformat()+'Z'}
    return _result(await asyncio.to_thread(counts))


async def notification_preferences(request):
    identity=await _identity(request)
    if request.method=='GET':return _result({'preferences':await asyncio.to_thread(alert_preferences.public_preferences,identity['user_id']),'channels':extra_channels.channel_status()})
    _site(request).check_rate('preferences:'+str(identity['user_id']),20,60)
    try:data=await asyncio.to_thread(alert_preferences.save_preferences,identity['user_id'],await _body(request))
    except ValueError as exc:return _result({'error':str(exc)},400)
    return _result({'preferences':data,'channels':extra_channels.channel_status(),'message':'Préférences d’alertes enregistrées.'})


async def notes(request):
    identity=await _identity(request)
    try:
        identifier=_identifier(request)
        if request.method=='GET':data=await asyncio.to_thread(property_tracking.note_for,identity['user_id'],identifier)
        else:
            _site(request).check_rate('notes:'+str(identity['user_id']),30,60)
            data=await asyncio.to_thread(property_tracking.save_note,identity['user_id'],identifier,await _body(request))
    except property_tracking.TrackingConflict as exc:return _result({'error':str(exc)},409)
    except PermissionError as exc:return _result({'error':str(exc)},403)
    except ValueError as exc:return _result({'error':str(exc)},400)
    return _result({'tracking':data})


async def prices(request):
    _,key=await _viewer(request)
    _site(request).check_rate('prices:'+key,60,60)
    try:
        limit=inventory.integer(request.query,'limit',50,100)
        if not limit:raise ValueError('La taille de page doit être positive.')
        data=await asyncio.to_thread(property_tracking.price_history,_identifier(request),before=inventory.integer(request.query,'before'),limit=limit)
    except ValueError as exc:return _result({'error':str(exc)},400)
    return _result(data)


async def notifications(request):
    identity=await _identity(request)
    try:
        if request.method=='GET':return _result(await asyncio.to_thread(member_notifications.inbox,identity['user_id'],before=request.query.get('before')))
        body=await _body(request)
        if not isinstance(body.get('id'),str) or len(body['id'])!=36:raise ValueError('Notification introuvable.')
        return _result(await asyncio.to_thread(member_notifications.mark_read,identity['user_id'],body['id']))
    except ValueError as exc:return _result({'error':str(exc)},404)


async def comparison(request):
    identity=await _identity(request)
    values=request.query.get('ids','').split(',')
    if not 2<=len(values)<=4 or any(not value.isdigit() or not 0<int(value)<=1000000000 for value in values):return _result({'error':'Sélectionnez de deux à quatre favoris.'},400)
    ids=[int(value) for value in values]
    def build():
        items=[];canonical=set()
        for identifier in ids:
            row=repo.get_listing_by_id(identifier)
            if not row or row.removed_at is not None or not repo.is_favorite(identity['user_id'],row.id):raise PermissionError('Le comparateur accepte uniquement vos favoris.')
            if row.id in canonical:raise ValueError('Sélectionnez des biens différents.')
            canonical.add(row.id)
            item=site_sync._listing(row,full=True,publications=repo.listing_publications(row.id))
            item['images']=item['images'][:1];item['description']='';item['freshness']=property_tracking.publication_freshness(row.id)
            tracking=property_tracking.note_for(identity['user_id'],row.id)
            item['personal_criteria']={key:tracking[key] for key in property_tracking.PERSONAL_FIELDS} if tracking else None
            items.append(item)
        return {'items':items}
    try:
        data=await asyncio.to_thread(build)
        if request.query.get('score')=='1':comparison_score.score_items(data['items'])
        return _result(data)
    except PermissionError as exc:return _result({'error':str(exc)},403)
    except ValueError as exc:return _result({'error':str(exc)},400)


async def commute_estimate(request):
    identity=await _identity(request)
    _site(request).check_rate('commute:'+str(identity['user_id']),20,60)
    body=await _body(request)
    try:
        row=await asyncio.to_thread(repo.get_listing_by_id,_identifier(request))
        if not row or row.removed_at is not None or row.inventory_status=='invalid':raise ValueError('Annonce introuvable.')
        return _result(await asyncio.to_thread(commute.estimate,row,body.get('destination'),body.get('mode')))
    except ValueError as exc:return _result({'error':str(exc)},400)


def _calendar(content):
    return web.Response(body=content.encode(),headers={'Content-Type':'text/calendar; charset=utf-8','Content-Disposition':'attachment; filename="visites-immoalert.ics"'})


async def calendar_all(request):
    identity=await _identity(request)
    _site(request).check_rate('calendar:'+str(identity['user_id']),20,60)
    content,_=await asyncio.to_thread(calendar_export.build,identity['user_id'])
    return _calendar(content)


async def calendar_one(request):
    identity=await _identity(request)
    _site(request).check_rate('calendar:'+str(identity['user_id']),20,60)
    try:content,count=await asyncio.to_thread(calendar_export.build,identity['user_id'],listing_id=_identifier(request))
    except ValueError as exc:return _result({'error':str(exc)},404)
    if not count:return _result({'error':'Aucune visite à venir pour ce bien.'},404)
    return _calendar(content)


async def shares(request):
    identity=await _identity(request);site=_site(request);user_id=identity['user_id']
    if request.method=='GET':return _result({'items':await asyncio.to_thread(sharing.list_for,user_id)})
    site.check_rate('shares:'+str(user_id),20,600)
    if request.method=='DELETE':
        try:
            value=request.match_info.get('identifier','')
            if not value.isdigit():raise ValueError('Lien introuvable.')
            await asyncio.to_thread(sharing.revoke,user_id,int(value))
            await asyncio.to_thread(audit.record,'share_revoked',user_id=user_id,actor=identity['login'] or '',detail=value)
        except ValueError as exc:return _result({'error':str(exc)},404)
        return _result({'message':'Lien révoqué.'})
    body=await _body(request)
    try:result=await asyncio.to_thread(sharing.create,user_id,body.get('ids'),body.get('title',''),body.get('days',7))
    except PermissionError as exc:return _result({'error':str(exc)},403)
    except ValueError as exc:return _result({'error':str(exc)},400)
    await asyncio.to_thread(audit.record,'share_created',user_id=user_id,actor=identity['login'] or '',detail=str(result['share']['id']))
    return _result(result,201)


async def shared_view(request):
    """Public and read-only: the unguessable token is the credential."""
    _,key=await _viewer(request)
    _site(request).check_rate('shared:'+key,30,60)
    data=await asyncio.to_thread(sharing.resolve,request.match_info.get('token',''))
    if data is None:return _result({'error':'Ce lien de partage est expiré ou révoqué.'},404)
    return _result(data)


async def push(request):
    identity=await _identity(request)
    _site(request).check_rate('push:'+str(identity['user_id']),20,600)
    if request.method=='GET':return _result(extra_channels.channel_status())
    body=await _body(request)
    try:
        if not extra_channels.push_available():raise ValueError('Les notifications push ne sont pas configurées sur ce serveur.')
        if request.method=='DELETE':await asyncio.to_thread(extra_channels.remove_subscription,identity['user_id'],str(body.get('endpoint','')))
        else:await asyncio.to_thread(extra_channels.save_subscription,identity['user_id'],body)
    except ValueError as exc:return _result({'error':str(exc)},400)
    return _result({'message':'Notifications activées sur cet appareil.' if request.method!='DELETE' else 'Notifications désactivées sur cet appareil.'})


async def client_performance(request):
    identity=await _identity(request)
    _site(request).check_rate('performance:'+str(identity['user_id']),120,600)
    body=await _body(request);samples=body.get('samples')
    if not isinstance(samples,list) or not 1<=len(samples)<=4:return _result({'error':'Mesures invalides.'},400)
    for sample in samples:
        if not isinstance(sample,dict) or sample.get('name') not in performance.CLIENT_METRICS or type(sample.get('value_ms')) not in (int,float) or not math.isfinite(sample['value_ms']) or not 0<=sample['value_ms']<=600000:
            return _result({'error':'Mesure invalide.'},400)
    for sample in samples:performance.record(sample['name'],sample['value_ms'])
    return _result({'accepted':len(samples)},202)


async def admin_performance(request):
    from services.web_api import require_admin
    await require_admin(request)
    def build():
        from services.favorite_freshness import freshness_plan
        data=performance.summary();data['freshness']=freshness_plan();return data
    return _result(await asyncio.to_thread(build))


async def detail_updates(request):
    identity,user_id=await _viewer(request);site=_site(request)
    site.check_rate('detail-updates:'+user_id,40,60)
    if site.detail_connections[user_id]>=4:return _result({'error':'Trop de fiches sont ouvertes. Réessayez dans quelques instants.'},429)
    try:
        identifier=_identifier(request)
        row=await asyncio.to_thread(repo.get_listing_by_id,identifier)
        if not row or row.removed_at is not None or row.inventory_status=='invalid':raise ValueError('Annonce introuvable.')
    except ValueError as exc:return _result({'error':str(exc)},404)
    from services.listing_details import get_listing_details
    site.detail_connections[user_id]+=1
    try:
        enriching=False
        try:row=await asyncio.wait_for(get_listing_details(identifier),timeout=18)
        except asyncio.TimeoutError:
            enriching=True;row=await asyncio.to_thread(repo.get_listing_by_id,identifier)
        # Recheck the account after waiting: revocation must not expose data.
        await _recheck(request,identity)
        return _result(await asyncio.to_thread(detail_payload,row,identity['user_id'] if identity else None,enriching))
    except ValueError as exc:return _result({'error':str(exc)},404)
    finally:site.detail_connections[user_id]-=1


async def detail_events(request):
    identity,user_id=await _viewer(request);site=_site(request)
    site.check_rate('detail-events:'+user_id,30,60)
    if site.detail_connections[user_id]>=4:return _result({'error':'Trop de fiches sont ouvertes.'},429)
    try:
        identifier=_identifier(request)
        row=await asyncio.to_thread(repo.get_listing_by_id,identifier)
        if not row or row.removed_at is not None or row.inventory_status=='invalid':raise ValueError('Annonce introuvable.')
    except ValueError as exc:return _result({'error':str(exc)},404)
    stream=web.StreamResponse(headers={'Content-Type':'text/event-stream; charset=utf-8','Cache-Control':'private, no-store','X-Accel-Buffering':'no','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer'})
    site.detail_connections[user_id]+=1
    from services.listing_details import get_listing_details
    async def send(event,data):await stream.write(('event: '+event+'\ndata: '+json.dumps(data,ensure_ascii=False)+'\n\n').encode())
    try:
        await stream.prepare(request);await send('connected',{'connected':True})
        started=perf_counter()
        while perf_counter()-started<120:
            if request.transport is None or request.transport.is_closing():break
            try:
                row=await asyncio.wait_for(get_listing_details(identifier),timeout=10)
                await _recheck(request,identity)
                await send('listing',await asyncio.to_thread(detail_payload,row,identity['user_id'] if identity else None,False))
                break
            except asyncio.TimeoutError:
                await _recheck(request,identity)
                await stream.write(b': waiting\n\n')
        else:await send('failed',{'error':'La récupération prend plus de temps que prévu. Vous pouvez réessayer.'})
        await stream.write_eof()
    except (ConnectionResetError,asyncio.CancelledError):
        pass
    except Exception:
        try:await send('failed',{'error':'La mise à jour de la fiche a été interrompue. Réessayez.'})
        except (ConnectionError,RuntimeError):pass
    finally:site.detail_connections[user_id]-=1
    return stream


def mount_routes(app):
    app.router.add_post('/api/listings/{identifier}/commute',commute_estimate)
    app.router.add_get('/api/listings/{identifier}/visit.ics',calendar_one)
    app.router.add_get('/api/calendar.ics',calendar_all)
    app.router.add_get('/api/shares',shares)
    app.router.add_post('/api/shares',shares)
    app.router.add_delete('/api/shares/{identifier}',shares)
    app.router.add_get('/api/shared/{token}',shared_view)
    app.router.add_get('/api/push',push)
    app.router.add_post('/api/push',push)
    app.router.add_delete('/api/push',push)
    app.router.add_get('/api/inventory',inventory_page)
    app.router.add_get('/api/tracking',tracking_page)
    app.router.add_get('/api/search-counts',search_counts)
    app.router.add_get('/api/comparison',comparison)
    app.router.add_get('/api/preferences',notification_preferences)
    app.router.add_post('/api/preferences',notification_preferences)
    app.router.add_get('/api/notifications',notifications)
    app.router.add_post('/api/notifications',notifications)
    app.router.add_post('/api/performance',client_performance)
    app.router.add_get('/api/admin/performance',admin_performance)
    app.router.add_get('/api/listings/{identifier}/area',location_area)
    app.router.add_get('/api/listings/{identifier}/note',notes)
    app.router.add_post('/api/listings/{identifier}/note',notes)
    app.router.add_get('/api/listings/{identifier}/prices',prices)
    app.router.add_get('/api/listings/{identifier}/updates',detail_updates)
    app.router.add_get('/api/listings/{identifier}/events',detail_events)
