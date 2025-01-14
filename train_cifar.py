import os
import pickle

import numpy as np
import torch
from sklearn.mixture import GaussianMixture
from cc_utils import ccgmm_codivide, gmm_codivide

from train import warmup, train


def save_losses(input_loss, exp):
    name = "./stats/cifar100/losses{}.pcl"
    nm = name.format(exp)
    if os.path.exists(nm):
        loss_history = pickle.load(open(nm, "rb"))
    else:
        loss_history, clean_history = [], []
    loss_history.append(input_loss)
    pickle.dump(loss_history, open(nm, "wb"))


def eval_train(
    model, eval_loader, CE, all_loss, epoch, net, device, r, stats_log, cclm
):
    model.eval()
    losses = torch.zeros(50000)
    losses_clean = torch.zeros(50000)
    targets_all = torch.zeros(50000, device=device)
    targets_clean_all = torch.zeros(50000, device=device)
    per_class_accuracy = np.zeros(100)

    with torch.no_grad():
        for batch_idx, (inputs, _, targets, index, targets_clean) in enumerate(
            eval_loader
        ):
            inputs, targets, targets_clean = (
                inputs.to(device),
                targets.to(device),
                targets_clean.to(device),
            )
            outputs = model(inputs)
            loss = CE(outputs, targets)
            clean_loss = CE(outputs, targets_clean)
            _, predicted = torch.max(outputs, 1)
            for c in set(predicted.cpu().numpy()):
                per_class_accuracy[c] += sum(predicted[targets_clean == c] == c)  
            for b in range(inputs.size(0)):
                losses[index[b]] = loss[b]
                targets_all[index[b]] = targets[b]
                losses_clean[index[b]] = clean_loss[b]
    losses = (losses - losses.min()) / (losses.max() - losses.min())
    all_loss.append(losses)

    history = torch.stack(all_loss)

    if r >= 0.9:  # average loss over last 5 epochs to improve convergence stability
        input_loss = history[-5:].mean(0)
        input_loss = input_loss.reshape(-1, 1)
    else:
        input_loss = losses.reshape(-1, 1)

    # exp = '_std_tpc_oracle'
    # save_losses(input_loss, exp)
    if not cclm:
        gmm = GaussianMixture(n_components=2, max_iter=200, tol=1e-2, reg_covar=5e-4)
        gmm.fit(input_loss)
        clean_idx, noisy_idx = gmm.means_.argmin(), gmm.means_.argmax()
        # stats_log.write(
        #     "Epoch {} (net {}): GMM results: {} with weight {}\t"
        #     "{} with weight {}\n".format(
        #         epoch,
        #         net,
        #         gmm.means_[clean_idx],
        #         gmm.weights_[clean_idx],
        #         gmm.means_[noisy_idx],
        #         gmm.weights_[noisy_idx],
        #     )
        # )
        # stats_log.flush()

        prob = gmm.predict_proba(input_loss)
        prob = prob[:, clean_idx]
    else:
        prob = ccgmm_codivide(input_loss, targets_all)

    per_class_accuracy /= 50000 / 100
    per_class_accuracy *= 100
    std = per_class_accuracy.std()
    acc = per_class_accuracy.mean()
    stats_log.write(
        f"Epoch:{epoch:d},Accuracy:{acc:.2f},STD:{std:.4f}\n"
    )
    stats_log.flush()

    return prob, all_loss, losses_clean


def run_test(epoch, net1, net2, test_loader, device, test_log):
    net1.eval()
    net2.eval()
    correct = 0
    total = 0
    num_class = 100
    per_class_accuracy = np.zeros(num_class)
    total_predicted = torch.zeros(10000, device=device)
    total_GT = torch.zeros(10000, device=device)
    with torch.no_grad():
        for batch_idx, (inputs, targets) in enumerate(test_loader):
            inputs, targets = inputs.to(device), targets.to(device)
            outputs1 = net1(inputs)
            outputs2 = net2(inputs)
            outputs = outputs1 + outputs2
            _, predicted = torch.max(outputs, 1)
            for c in set(predicted.cpu().numpy()):
                per_class_accuracy[c] += sum(predicted[targets == c] == c)

            total += targets.size(0)
            correct += predicted.eq(targets).cpu().sum().item()
    acc = 100.0 * correct / total
    per_class_accuracy /= total / num_class
    std = per_class_accuracy.std()
    print("\n| Test Epoch #%d\t Accuracy: %.2f%%\t STD: %.4f%%\n" % (epoch, acc, std))
    test_log.write("Epoch:%d   Accuracy:%.2f\t STD: %.4f%%\n" % (epoch, acc, std))
    test_log.flush()
    return acc


def run_train_loop(
    net1,
    optimizer1,
    sched1,
    net2,
    optimizer2,
    sched2,
    criterion,
    CEloss,
    CE,
    loader,
    p_threshold,
    warm_up,
    num_epochs,
    all_loss,
    batch_size,
    num_class,
    device,
    lambda_u,
    T,
    alpha,
    noise_mode,
    dataset,
    r,
    conf_penalty,
    stats_log,
    loss_log,
    test_log,
    memory_log,
    cclm,
    args,
):
    for epoch in range(num_epochs + 1):
        test_loader = loader.run("test")
        eval_loader = loader.run("eval_train")

        if epoch < warm_up:
            warmup_trainloader = loader.run("warmup")
            print("Warmup Net1")
            warmup(
                epoch,
                net1,
                optimizer1,
                warmup_trainloader,
                CEloss,
                conf_penalty,
                device,
                dataset,
                r,
                num_epochs,
                noise_mode,
            )
            print("\nWarmup Net2")
            warmup(
                epoch,
                net2,
                optimizer2,
                warmup_trainloader,
                CEloss,
                conf_penalty,
                device,
                dataset,
                r,
                num_epochs,
                noise_mode,
            )

            # prob1, all_loss[0], losses_clean1 = eval_train(
            #     net1, eval_loader, CE, all_loss[0], epoch, 1, device, r, stats_log, cclm
            # )
            # prob2, all_loss[1], losses_clean2 = eval_train(
            #     net2, eval_loader, CE, all_loss[1], epoch, 2, device, r, stats_log, cclm
            # )

            # p_thr2 = np.clip(p_threshold, prob2.min() + 1e-5, prob2.max() - 1e-5)
            # pred2 = prob2 > p_thr2

            # loss_log.write(
            #     "{},{},{},{},{}\n".format(
            #         epoch,
            #         losses_clean2[pred2].mean(),
            #         losses_clean2[pred2].std(),
            #         losses_clean2[~pred2].mean(),
            #         losses_clean2[~pred2].std(),
            #     )
            # )
            # loss_log.flush()
            # loader.run("train", pred2, prob2)  # count metrics
        else:
            print("Train Net1")
            prob2, all_loss[1], losses_clean2 = eval_train(
                net2,
                eval_loader,
                CE,
                all_loss[1],
                epoch,
                2,
                device,
                r,
                stats_log,
                cclm,
            )

            p_thr2 = np.clip(p_threshold, prob2.min() + 1e-5, prob2.max() - 1e-5)
            pred2 = prob2 > p_thr2

            loss_log.write(
                "{},{},{},{},{}\n".format(
                    epoch,
                    losses_clean2[pred2].mean(),
                    losses_clean2[pred2].std(),
                    losses_clean2[~pred2].mean(),
                    losses_clean2[~pred2].std(),
                )
            )
            loss_log.flush()

            labeled_trainloader, unlabeled_trainloader = loader.run(
                "train", pred2, prob2
            )  # co-divide
            train(
                epoch,
                net1,
                net2,
                criterion,
                optimizer1,
                labeled_trainloader,
                unlabeled_trainloader,
                lambda_u,
                batch_size,
                num_class,
                device,
                T,
                alpha,
                warm_up,
                dataset,
                r,
                noise_mode,
                num_epochs,
            )  # train net1

            print("\nTrain Net2")
            prob1, all_loss[0], losses_clean1 = eval_train(
                net1,
                eval_loader,
                CE,
                all_loss[0],
                epoch,
                1,
                device,
                r,
                stats_log,
                cclm,
            )

            p_thr1 = np.clip(p_threshold, prob1.min() + 1e-5, prob1.max() - 1e-5)
            pred1 = prob1 > p_thr1

            labeled_trainloader, unlabeled_trainloader = loader.run(
                "train", pred1, prob1
            )  # co-divide
            train(
                epoch,
                net2,
                net1,
                criterion,
                optimizer2,
                labeled_trainloader,
                unlabeled_trainloader,
                lambda_u,
                batch_size,
                num_class,
                device,
                T,
                alpha,
                warm_up,
                dataset,
                r,
                noise_mode,
                num_epochs,
            )  # train net2

        this_acc = run_test(epoch, net1, net2, test_loader, device, test_log)

        if (not epoch % 25) or (epoch == warm_up) or epoch == 0:
            checkpoint1 = {
                "net": net1.state_dict(),
                "Model_number": 1,
                "Noise_Ratio": args.r,
                "Loss Function": "CrossEntropyLoss",
                "Optimizer": "SGD",
                "Noise_mode": args.noise_mode,
                "Accuracy": this_acc,
                "Dataset": args.dataset,
                "Batch Size": args.batch_size,
                "epoch": epoch,
            }
            checkpoint2 = {
                "net": net2.state_dict(),
                "Model_number": 2,
                "Noise_Ratio": args.r,
                "Loss Function": "CrossEntropyLoss",
                "Optimizer": "SGD",
                "Noise_mode": args.noise_mode,
                "Accuracy": this_acc,
                "Dataset": args.dataset,
                "Batch Size": args.batch_size,
                "epoch": epoch,
            }

            torch.save(checkpoint1, f"{memory_log}/{epoch}_1.pt")
            torch.save(checkpoint2, f"{memory_log}/{epoch}_2.pt")

            sched1.step()
            sched2.step()

    torch.save(net1.state_dict(), "./final_checkpoints/final_checkpoint.pth.tar")
