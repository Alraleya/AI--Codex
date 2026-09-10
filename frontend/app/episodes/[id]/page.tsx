import EpisodeWorkbench from "./workbench";

export default async function EpisodePage({
  params,
  searchParams,
}: {
  params: Promise<{ id: string }>;
  searchParams: Promise<{ project?: string }>;
}) {
  const { id } = await params;
  const { project = "" } = await searchParams;
  return <EpisodeWorkbench episodeId={id} projectId={project} />;
}
