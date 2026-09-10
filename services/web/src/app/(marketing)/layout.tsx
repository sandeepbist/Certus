import { Navbar } from '@/components/marketing/Navbar';
import { Footer } from '@/components/marketing/Footer';

export default function MarketingLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <div className="min-h-screen flex flex-col bg-black text-zinc-100 selection:bg-white/20 selection:text-white">
      <Navbar />
      <main className="flex-1">{children}</main>
      <Footer />
    </div>
  );
}
