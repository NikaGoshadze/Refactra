import { RepositoryConnection } from "@/components/repository-connection";
import type { Metadata } from "next";
import { TopBar } from "@/components/top-bar";

export const metadata: Metadata = { title: "Workspace · Refactra" };

export default function Home() {
  return (
    <div className="workspace">
      <TopBar />
      <main>
        <RepositoryConnection>
          <h1 className="page-title">Workspace</h1>
        </RepositoryConnection>
      </main>
    </div>
  );
}
