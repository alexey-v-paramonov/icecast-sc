#!/usr/bin/env python3
"""Exercise production XSL cache functions with real libxslt and pthread locks.

No server, station config, or system changes. Requires cc and pkg-config libxslt.
"""
from pathlib import Path
import shlex
import subprocess
import tempfile


def extract(source, signature):
    start = source.index(signature)
    return source[start:source.index('\n}', start) + 2]


repo = Path(__file__).resolve().parents[2]
source = (repo/'src/xslt.c').read_text()
types = source[source.index('typedef struct {'):source.index('static int xslt_client')]
methods = '\n'.join(extract(source, sig) for sig in (
    'static void clear_cached_stylesheet', 'static void xsl_req_clear',
    'static int xslt_cached', 'static int _apply_sheet', 'static int xslt_apply_sheet'))
fixture = r'''
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <sys/stat.h>
#include <pthread.h>
#include <libxml/parser.h>
#include <libxslt/transform.h>
#include <libxslt/xsltutils.h>
#define XMLSTR(x) ((xmlChar*)(x))
#define DEBUG1(...) ((void)0)
#define DEBUG2(...) ((void)0)
#define WARN1(...) ((void)0)
#define WARN2(...) ((void)0)
#define INFO1(...) ((void)0)
#define filename_cmp strcmp
typedef struct avl_node {void *key;} avl_node;
typedef struct {int length;} avl_tree;
static avl_node *avl_get_first(avl_tree *t) {return NULL;}
static avl_node *avl_get_next(avl_node *t) {return NULL;}
typedef struct {char *name,*value;} http_var_t;
typedef struct {avl_tree *queryvars;} parser_t;
typedef struct {parser_t *parser; uintptr_t aux_data;} client_t;
static char *util_url_escape(const char *s) {return strdup(s);}
static void fserve_write_mime_ext(const char *s,char *p,size_t n) {snprintf(p,n,"txt");}
#define thread_mutex_lock pthread_mutex_lock
#define thread_mutex_unlock pthread_mutex_unlock
#define thread_rwlock_rlock pthread_rwlock_rdlock
#define thread_rwlock_wlock pthread_rwlock_wrlock
#define thread_rwlock_unlock pthread_rwlock_unlock
''' + types + r'''
#define CACHESIZE 20
static stylesheet_cache_t cache[CACHESIZE];
static pthread_mutex_t cache_lock=PTHREAD_MUTEX_INITIALIZER;
static pthread_rwlock_t sheet_lock=PTHREAD_RWLOCK_INITIALIZER;
''' + methods + r'''
static xsl_req *request(const char *file) {
  xsl_req *x=calloc(1,sizeof(*x));x->filename=strdup(file);return x;
}
static void *expire_waiter(void *arg) { xsl_req_clear(arg);return NULL; }
int main(int argc,char **argv) {
  xmlInitParser();
  // A second request expires on another thread while the loader owns the slot.
  xsl_req *loader=request(argv[1]),*waiter=request(argv[1]);
  assert(xslt_cached(loader,100)==1);
  assert(xslt_cached(waiter,101)==1);
  assert(waiter->flags & XSLREQ_DELAY);
  pthread_t t;pthread_create(&t,NULL,expire_waiter,waiter);pthread_join(t,NULL);
  assert(cache[0].flags & XSLCACHE_PENDING);
  assert(cache[0].refc==1);
  // Expiring the loader permits a new owner, without a minute of null-sheet retries.
  xsl_req_clear(loader);
  loader=request(argv[1]);assert(xslt_cached(loader,102)==1);
  assert(loader->flags & XSLREQ_CACHE_OWNER);
  parser_t parser={0};client_t client={.parser=&parser};loader->client=&client;
  // Force the null-sheet error path and prove a writer can still take the lock.
  cache[0].next_check=1000;
  pthread_mutex_lock(&cache_lock);
  assert(xslt_apply_sheet(loader,103)==-1);
  assert(pthread_rwlock_trywrlock(&sheet_lock)==0);
  pthread_rwlock_unlock(&sheet_lock);xsl_req_clear(loader);
  // A failed/missing stylesheet recovers when the file is restored.
  clear_cached_stylesheet(&cache[0],1);
  loader=request("/nonexistent/xslt-cache-test.xsl");
  assert(xslt_cached(loader,200)==1);loader->client=&client;
  pthread_mutex_lock(&cache_lock);assert(xslt_apply_sheet(loader,200)==-1);
  xsl_req_clear(loader);clear_cached_stylesheet(&cache[0],1);
  loader=request(argv[1]);assert(xslt_cached(loader,300)==1);
  loader->client=&client;
  const char *xml="<icestats><source mount='/stream'><listeners>2</listeners><listener_peak>7</listener_peak></source></icestats>";
  loader->doc=xmlReadMemory(xml,strlen(xml),"test.xml",NULL,0);
  pthread_mutex_lock(&cache_lock);assert(xslt_apply_sheet(loader,300)==0);
  xmlChar *out=NULL;int len=0;
  assert(xsltSaveResultToString(&out,&len,loader->doc,cache[0].stylesheet)==0);
  assert(len==11 && memcmp(out,"/stream:2:7",11)==0);xmlFree(out);
  pthread_rwlock_unlock(&sheet_lock);xsl_req_clear(loader);
  assert(!(cache[0].flags & XSLCACHE_PENDING) && cache[0].refc==0);
  clear_cached_stylesheet(&cache[0],1);
  puts("PASS: waiter/loader expiry, retry ownership, null-sheet lock release, real XSL transform");
}
'''
with tempfile.TemporaryDirectory(prefix='xslt-cache-') as tmp:
    tmp = Path(tmp)
    (tmp/'test.c').write_text(fixture)
    (tmp/'load.xsl').write_text('''<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform"><xsl:output method="text"/><xsl:template match="/icestats"><xsl:for-each select="source"><xsl:value-of select="@mount"/>:<xsl:value-of select="listeners"/>:<xsl:value-of select="listener_peak"/></xsl:for-each></xsl:template></xsl:stylesheet>''')
    libs = shlex.split(subprocess.check_output(['pkg-config','--cflags','--libs','--static','libxslt'],text=True))
    subprocess.run(['cc','-O1','-g','-pthread',str(tmp/'test.c'),'-o',str(tmp/'test'),*libs,'-lm'],check=True)
    subprocess.run([str(tmp/'test'),str(tmp/'load.xsl')],check=True,timeout=10)
