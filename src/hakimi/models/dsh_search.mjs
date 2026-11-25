/** Read a single query with the installed DSH web provider. No agent or tools. */
export const name = 'hakimi-dsh-search';
export const inject = ['web', 'headlessStartup'];

export function apply(ctx) {
  async function search() {
    await ctx.get('loader')?.await();
    const task = ctx.get('headlessStartup').task;
    const query = task.slice('Search: '.length);
    const result = await ctx.get('web').search({ query, maxResults: 8 });
    const sources = result.sources.map(({ url, title, snippet, publishedAt }) => ({
      url: url.slice(0, 8193), title: title?.slice(0, 300),
      snippet: snippet?.slice(0, 1100), date: publishedAt?.slice(0, 80)
    })).filter(source => source.url.length <= 8192);
    return {sources};
  }
  const finish = async result => {
    await new Promise(resolve => process.stdout.write('HAKIMI_SEARCH ' + JSON.stringify(result) + '\n', resolve));
    ctx.get('appExit')(0);
  };
  search().then(finish, () => finish({error: 'dsh_search_unavailable'}));
}
